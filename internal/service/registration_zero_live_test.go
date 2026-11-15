package service

import (
	"bytes"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"

	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/testsupport"
)

func TestRegistrationCatalogZeroASR(t *testing.T) {
	f := canonicalModelDatabase(t)
	cfg := server.GetConfig()
	priorDefaults, priorEnabled := cfg.UserDefaultLLM.DefaultModels, cfg.Authentication.RegisterEnabled
	t.Cleanup(func() {
		cfg.UserDefaultLLM.DefaultModels = priorDefaults
		cfg.Authentication.RegisterEnabled = priorEnabled
	})
	cfg.Authentication.RegisterEnabled = true
	cfg.UserDefaultLLM.DefaultModels = server.DefaultModelsConfig{ASRModel: server.ModelConfig{Factory: "Groq", Name: "whisper-large-v3", APIKey: "controlled-asr-key", BaseURL: "https://controlled.invalid/v1"}}
	active := "1"
	if err := f.DB.Create(&entity.LLM{LLMName: "whisper-large-v3", FID: "Groq", ModelType: "speech2text", Tags: "ASR", MaxTokens: 0, Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	directory := t.TempDir()
	keyPath := filepath.Join(directory, "conf", "private.pem")
	f.Journal.Event("reserve-registration-key", map[string]any{"path": keyPath})
	if err := os.Mkdir(filepath.Dir(keyPath), 0700); err != nil {
		t.Fatal(err)
	}
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(keyPath, pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)}), 0600); err != nil {
		t.Fatal(err)
	}
	t.Chdir(directory)
	encrypt := func(value string) string {
		t.Helper()
		raw, err := rsa.EncryptPKCS1v15(rand.Reader, &key.PublicKey, []byte(value))
		if err != nil {
			t.Fatal(err)
		}
		return base64.StdEncoding.EncodeToString(raw)
	}
	userService := NewUserService()
	snapshot := func() []int64 {
		t.Helper()
		out := make([]int64, 0, 8)
		for _, table := range []string{"user", "tenant", "user_tenant", "file", "tenant_llm", "tenant_model_provider", "tenant_model_instance", "tenant_model"} {
			var count int64
			if err := f.Read.QueryRow("SELECT COUNT(*) FROM `" + table + "`").Scan(&count); err != nil {
				t.Fatal(err)
			}
			out = append(out, count)
		}
		return out
	}
	t.Run("registration_rollback_and_zero", func(t *testing.T) {
		request := &RegisterRequest{Email: "registered-zero@example.invalid", Nickname: "zero", Password: encrypt("controlled-password")}
		f.Journal.Event("reserve-registration-trigger", map[string]any{"name": "reject_zero_registration_file"})
		if err := f.DB.Exec("CREATE TRIGGER reject_zero_registration_file BEFORE INSERT ON file FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='controlled registration failure'").Error; err != nil {
			t.Fatal(err)
		}
		before := snapshot()
		user, code, err := userService.Register(request)
		if err == nil || code == common.CodeSuccess || user != nil || !reflect.DeepEqual(before, snapshot()) {
			t.Fatal("late registration failure persisted account/defaults", code, err)
		}
		if err := f.DB.Exec("DROP TRIGGER reject_zero_registration_file").Error; err != nil {
			t.Fatal(err)
		}
		user, code, err = userService.Register(request)
		if err != nil || code != common.CodeSuccess || user == nil {
			t.Fatal("registration failed", code, err)
		}
		var reference, hash string
		var tokens int64
		if err := f.Read.QueryRow("SELECT asr_id FROM tenant WHERE id=?", user.ID).Scan(&reference); err != nil || reference != "whisper-large-v3" {
			t.Fatal("original registration default contract", reference, err)
		}
		if err := f.Read.QueryRow("SELECT max_tokens FROM tenant_llm WHERE tenant_id=? AND llm_factory=? AND llm_name=?", user.ID, "Groq", "whisper-large-v3").Scan(&tokens); err != nil || tokens != 0 {
			t.Fatal("legal zero became an invented budget", tokens, err)
		}
		if err := f.Read.QueryRow("SELECT password FROM user WHERE id=?", user.ID).Scan(&hash); err != nil || !userService.VerifyPassword(hash, "controlled-password") {
			t.Fatal("registered password could not be verified", err)
		}
		var count int
		if err := f.Read.QueryRow("SELECT COUNT(*) FROM tenant_model_provider WHERE tenant_id=?", user.ID).Scan(&count); err != nil || count != 0 {
			t.Fatal("registration invented a new-model binding", count, err)
		}
	})
	t.Run("invalid_RSA_has_no_writes", func(t *testing.T) {
		for i, password := range []string{"not-base64", base64.StdEncoding.EncodeToString([]byte("invalid-ciphertext")), ""} {
			before := snapshot()
			user, code, err := userService.Register(&RegisterRequest{Email: fmt.Sprintf("bad-rsa-%d@example.invalid", i), Nickname: "invalid", Password: password})
			if err == nil || code == common.CodeSuccess || user != nil || !reflect.DeepEqual(before, snapshot()) {
				t.Fatal("invalid RSA created account or default rows", i, code, err)
			}
		}
	})
	t.Run("canonical_model_binding_zero_ASR", func(t *testing.T) {
		audio := []byte("RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x40\x1f\x00\x00\x80\x3e\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00")
		var calls atomic.Int32
		upstream := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls.Add(1)
			if r.URL.Path != "/audio/transcriptions" || r.Header.Get("Authorization") != "Bearer controlled-asr-key" || r.ParseMultipartForm(1<<20) != nil {
				t.Error("original ASR request contract", r.URL)
				w.WriteHeader(400)
				return
			}
			file, _, err := r.FormFile("file")
			if err != nil {
				t.Error(err)
				return
			}
			defer file.Close()
			data, err := io.ReadAll(file)
			if err != nil || !bytes.Equal(data, audio) || r.FormValue("max_tokens") != "" {
				t.Error("ASR content or legal zero changed")
			}
			fmt.Fprint(w, `{"text":"zero works"}`)
		}))
		svc := NewModelProviderService()
		owner := "model-owner"
		provider, instance, name := "Groq", "zero", "whisper-large-v3"
		if code, err := svc.AddModelProvider(provider, owner); err != nil || code != common.CodeSuccess {
			t.Fatal(code, err)
		}
		if code, err := svc.CreateProviderInstance(provider, instance, "controlled-asr-key", upstream.URL, "default", owner); err != nil || code != common.CodeSuccess {
			t.Fatal(code, err)
		}
		request := &AddModelRequest{ProviderName: provider, InstanceName: instance, Models: []ModelRequest{{ModelName: name, ModelTypes: []string{"asr"}, MaxTokens: 0}}}
		if code, err := svc.AddModel(request, owner); err != nil || code != common.CodeSuccess {
			t.Fatal(code, err)
		}
		beforeDeclaration := snapshot()
		negative := &AddModelRequest{ProviderName: provider, InstanceName: instance, Models: []ModelRequest{{ModelName: "invalid-negative-asr", ModelTypes: []string{"asr"}, MaxTokens: -1}}}
		if code, err := svc.AddModel(negative, owner); err == nil || code == common.CodeSuccess || !reflect.DeepEqual(beforeDeclaration, snapshot()) || calls.Load() != 0 {
			t.Fatal("negative model declaration changed SQL or reached supplier", code, err)
		}
		var extra string
		if err := f.Read.QueryRow("SELECT extra FROM tenant_model WHERE model_name=?", name).Scan(&extra); err != nil {
			t.Fatal(err)
		}
		var declaration map[string]any
		if json.Unmarshal([]byte(extra), &declaration) != nil || declaration["max_tokens"] != float64(0) {
			t.Fatal("zero custom declaration not persisted")
		}
		encoded := base64.StdEncoding.EncodeToString(audio)
		result, code, err := svc.TranscribeAudio(&provider, &instance, &name, nil, owner, &encoded, nil, nil)
		if err != nil || code != common.CodeSuccess || result == nil || result.Text != "zero works" || calls.Load() != 1 {
			t.Fatal("actual original ASR failed", code, err)
		}
		if code, err := svc.UpdateModelStatus(provider, instance, name, owner, "inactive"); err != nil || code != common.CodeSuccess {
			t.Fatal(code, err)
		}
		before := snapshot()
		beforeCalls := calls.Load()
		if result, _, err := svc.TranscribeAudio(&provider, &instance, &name, nil, owner, &encoded, nil, nil); err == nil || result != nil || calls.Load() != beforeCalls || !reflect.DeepEqual(before, snapshot()) {
			t.Fatal("disabled model reached supplier or changed SQL", err)
		}
		if strings.Contains(extra, "controlled-asr-key") {
			t.Fatal("model declaration duplicated credentials")
		}
	})
}
