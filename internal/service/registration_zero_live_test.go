package service

import (
	"bytes"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"database/sql"
	"encoding/base64"
	"encoding/pem"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/server"
	"multirag/internal/testsupport"
)

// TestRegistrationCatalogZeroASR preserves zero/omitted catalog limits without
// loosening custom declarations or creating an empty disabled override.
func TestRegistrationCatalogZeroASR(t *testing.T) {
	config, address := os.Getenv("MULTIRAG_BATCH_G_CONFIG"), os.Getenv("MULTIRAG_BATCH_G_ZERO_ADDR")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned scratch PostgreSQL required")
		}
		t.Skip("requires owned scratch PostgreSQL")
	}
	if address == "" {
		t.Fatal("exact listener must be reserved before creation")
	}
	root := filepath.Join("..", "..")
	if _, err := os.Stat("go.mod"); err == nil {
		root = "."
	}
	t.Chdir(root)
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal("private config unavailable")
	}
	cfg := server.GetConfig()
	if cfg.Database.Host != "127.0.0.1" || !strings.HasPrefix(cfg.Database.Database, "multirag_batch_g_") {
		t.Fatal("unowned database")
	}
	journal := testsupport.NewCase(t)
	journal.Event("before-schema", map[string]any{"database": cfg.Database.Database})
	if err := dao.InitDB(); err != nil {
		t.Fatal(err)
	}
	pool, _ := dao.DB.DB()
	defer pool.Close()
	dsn, err := dao.PostgresDSN(cfg.Database)
	if err != nil {
		t.Fatal(err)
	}
	read, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer read.Close()
	snapshot := func() string {
		t.Helper()
		var result string
		if err := read.QueryRow("SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text,'[]') FROM (SELECT id,email FROM t_ai_users UNION ALL SELECT id,provider_name FROM tenant_model_provider UNION ALL SELECT id,instance_name FROM tenant_model_instance UNION ALL SELECT id,model_name FROM tenant_model UNION ALL SELECT id,asr_id FROM t_ai_tenants UNION ALL SELECT id,role FROM t_ai_user_tenants UNION ALL SELECT id,name FROM t_ai_files) t").Scan(&result); err != nil {
			t.Fatal(err)
		}
		return result
	}
	var calls atomic.Int64
	var srv *httptest.Server
	t.Cleanup(func() {
		if srv == nil {
			return
		}
		srv.Close()
		conn, err := net.DialTimeout("tcp", address, 200*time.Millisecond)
		if err == nil {
			conn.Close()
			t.Error("listener remains")
		}
		journal.Event("listener-cleanup-readback", map[string]any{"address": address, "absent": err != nil})
	})
	journal.Event("reserve-listener", map[string]any{"address": address})
	listener, err := net.Listen("tcp", address)
	if err != nil {
		t.Fatal(err)
	}
	audio := []byte("RIFF-owned-wave")
	srv = &httptest.Server{Listener: listener, Config: &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/v1/audio/transcriptions" || r.Header.Get("Authorization") != "Bearer owned-asr-key" {
			t.Error("wrong ASR route or credential")
			w.WriteHeader(400)
			return
		}
		if err := r.ParseMultipartForm(32 << 20); err != nil {
			t.Error(err)
			w.WriteHeader(400)
			return
		}
		if r.FormValue("max_tokens") != "" {
			t.Error("invented ASR token budget")
		}
		switch r.FormValue("model") {
		case "whisper-large-v3", "whisper-large-v3-turbo":
		default:
			t.Error("wrong ASR model")
		}
		file, _, err := r.FormFile("file")
		if err != nil {
			t.Error(err)
			w.WriteHeader(400)
			return
		}
		data, _ := io.ReadAll(file)
		file.Close()
		if !bytes.Equal(data, audio) {
			t.Error("audio changed")
		}
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, `{"text":"catalog zero works"}`)
	})}}
	srv.Start()
	keyData, err := os.ReadFile("configs/private.pem")
	if err != nil {
		t.Fatal(err)
	}
	block, _ := pem.Decode(keyData)
	der := block.Bytes
	if block.Headers["Proc-Type"] != "" {
		der, err = x509.DecryptPEMBlock(block, []byte("Welcome"))
		if err != nil {
			t.Fatal(err)
		}
	}
	key, err := x509.ParsePKCS1PrivateKey(der)
	if err != nil {
		t.Fatal(err)
	}
	encrypted, err := rsa.EncryptPKCS1v15(rand.Reader, &key.PublicKey, []byte(base64.StdEncoding.EncodeToString([]byte("owned-zero-password"))))
	if err != nil {
		t.Fatal(err)
	}
	manager := dao.GetModelProviderManager()
	resolver := NewModelProviderService()
	us := NewUserService()
	for index, name := range []string{"whisper-large-v3", "whisper-large-v3-turbo"} {
		t.Run(name, func(t *testing.T) {
			definition, err := manager.GetModelByName("Groq", name)
			if err != nil || definition.MaxTokens != 0 {
				t.Fatal("catalog zero precondition", err)
			}
			cfg.UserDefaultLLM.DefaultModels = server.DefaultModelsConfig{ASRModel: server.ModelConfig{Factory: "Groq", Name: name, APIKey: "owned-asr-key", BaseURL: srv.URL + "/v1"}}
			req := &RegisterRequest{Email: name + "@owned.invalid", Nickname: "zero", Password: base64.StdEncoding.EncodeToString(encrypted)}
			if index == 0 {
				if _, err := read.Exec("ALTER TABLE t_ai_files ADD CONSTRAINT zero_asr_reject_root CHECK (name != '/')"); err != nil {
					t.Fatal(err)
				}
				before := snapshot()
				if _, code, err := us.Register(req); err == nil || code != common.CodeServerError {
					t.Fatal("late SQL failure accepted")
				}
				if snapshot() != before {
					t.Fatal("zero catalog failure left account/model rows")
				}
				if _, err := read.Exec("ALTER TABLE t_ai_files DROP CONSTRAINT zero_asr_reject_root"); err != nil {
					t.Fatal(err)
				}
			}
			user, code, err := us.Register(req)
			if err != nil || code != 0 {
				t.Fatal("legitimate zero catalog registration rejected", code, err)
			}
			var ref string
			var overrides int
			if err := read.QueryRow("SELECT asr_id FROM t_ai_tenants WHERE id=$1", user.ID).Scan(&ref); err != nil || ref != name+"@default-asr@Groq" {
				t.Fatal("default ASR reference", ref, err)
			}
			if err := read.QueryRow("SELECT count(*) FROM tenant_model m JOIN tenant_model_provider p ON p.id=m.provider_id WHERE p.tenant_id=$1", user.ID).Scan(&overrides); err != nil || overrides != 0 {
				t.Fatal("catalog zero created a custom/disabled override", overrides, err)
			}
			driver, resolvedName, api, err := resolver.getModelConfig(user.ID, "", entity.ModelTypeSpeech2Text)
			if err != nil || resolvedName != name {
				t.Fatal("default zero resolver failed", err)
			}
			capability, ok := driver.(models.ASRDriver)
			if !ok {
				t.Fatal("ASR capability missing")
			}
			result, err := capability.TranscribeAudio(&resolvedName, audio, api, nil)
			if err != nil || result == nil || result.Text == nil || *result.Text != "catalog zero works" {
				t.Fatal("controlled default ASR failed", err)
			}
			if definition.MaxTokens != 0 {
				t.Fatal("catalog zero mutated")
			}
			if code, err := resolver.UpdateModelStatus("Groq", "default-asr", name, user.ID, "inactive"); err != nil || code != 0 {
				t.Fatal(err)
			}
			beforeCalls := calls.Load()
			if _, _, _, err := resolver.getModelConfig(user.ID, "", entity.ModelTypeSpeech2Text); !errors.Is(err, errModelDisabled) {
				t.Fatal("catalog disable marker bypassed", err)
			}
			if calls.Load() != beforeCalls {
				t.Fatal("disabled model contacted provider")
			}
			if code, err := resolver.UpdateModelStatus("Groq", "default-asr", name, user.ID, "active"); err != nil || code != 0 {
				t.Fatal(err)
			}
			if _, _, _, err := resolver.getModelConfig(user.ID, "", entity.ModelTypeSpeech2Text); err != nil {
				t.Fatal("catalog re-enable failed", err)
			}
			journal.Event("zero-asr-contract", map[string]any{"model": name, "maxTokens": definition.MaxTokens, "defaultResolver": true, "controlledMultipart": true, "disabledProviderCalls": 0})
		})
	}
	if _, err := decodeCustomModelExtra(`{"max_tokens":0,"model_types":["asr"]}`); err == nil {
		t.Fatal("custom positive token contract weakened")
	}
	if calls.Load() != 2 {
		t.Fatal("unexpected controlled ASR call count", calls.Load())
	}
}
