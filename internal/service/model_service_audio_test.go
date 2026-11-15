package service

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gabriel-vasile/mimetype"
	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/testsupport"
)

func TestCanonicalASRPayloadBoundarySQLHTTP(t *testing.T) {
	s := canonicalModelDatabase(t)
	journal := testsupport.NewCase(t)
	directory := filepath.Join(os.TempDir(), "asr-payload-"+s.Prefix)
	journal.Event("reserve", map[string]any{"kind": "directory", "path": directory})
	if err := os.Mkdir(directory, 0700); err != nil {
		t.Fatal(err)
	}
	journal.Event("allocated", map[string]any{"kind": "directory", "path": directory})
	t.Cleanup(func() {
		if err := os.RemoveAll(directory); err != nil {
			t.Error(err)
		}
		_, err := os.Stat(directory)
		journal.Event("cleanup-readback", map[string]any{"kind": "directory", "path": directory, "absent": os.IsNotExist(err)})
	})
	t.Setenv("TMPDIR", directory)
	// Minimal RIFF/WAVE header keeps the bytes and the MIME-derived file suffix independently observable.
	audio := []byte("RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x40\x1f\x00\x00\x80\x3e\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00")
	encoded := base64.StdEncoding.EncodeToString(audio)
	var calls atomic.Int32
	var stall atomic.Bool
	started, stopped := make(chan struct{}, 1), make(chan struct{}, 1)
	upstream := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/audio/transcriptions" || r.Header.Get("Authorization") != "Bearer controlled-audio-key" {
			t.Error("ASR endpoint or credential binding changed")
		}
		if err := r.ParseMultipartForm(1 << 20); err != nil {
			t.Error(err)
			w.WriteHeader(400)
			return
		}
		defer r.MultipartForm.RemoveAll()
		file, header, err := r.FormFile("file")
		if err != nil {
			t.Error(err)
			w.WriteHeader(400)
			return
		}
		defer file.Close()
		raw, err := io.ReadAll(file)
		if err != nil || string(raw) != string(audio) || filepath.Ext(header.Filename) != mimetype.Detect(audio).Extension() {
			t.Error("base64 bytes or detected suffix changed")
		}
		entries, err := os.ReadDir(directory)
		if err != nil || len(entries) != 1 {
			t.Error("request did not own exactly one temporary audio file")
		} else {
			info, err := entries[0].Info()
			if err != nil || info.Mode().Perm() != 0600 {
				t.Error("temporary audio was not private")
			}
			journal.Event("observed", map[string]any{"kind": "file", "path": filepath.Join(directory, entries[0].Name()), "mode": "0600"})
		}
		if stall.Load() {
			w.WriteHeader(200)
			w.(http.Flusher).Flush()
			started <- struct{}{}
			<-r.Context().Done()
			stopped <- struct{}{}
			return
		}
		fmt.Fprint(w, `{"text":"spoken"}`)
	}))
	svc := NewModelProviderService()
	if code, err := svc.AddModelProvider("OpenAI", "model-owner"); err != nil || code != common.CodeSuccess {
		t.Fatal(code, err)
	}
	if code, err := svc.CreateProviderInstance("OpenAI", "default", "controlled-audio-key", upstream.URL, "default", "model-owner"); err != nil || code != common.CodeSuccess {
		t.Fatal(code, err)
	}
	if code, err := svc.AddModel(&AddModelRequest{ProviderName: "OpenAI", InstanceName: "default", Models: []ModelRequest{{ModelName: "whisper-1", ModelTypes: []string{"asr"}}, {ModelName: "gpt-4o", ModelTypes: []string{"chat"}}}}, "model-owner"); err != nil || code != common.CodeSuccess {
		t.Fatal(code, err)
	}
	var model, chat entity.TenantModel
	if err := s.DB.Where("model_name = ?", "whisper-1").First(&model).Error; err != nil {
		t.Fatal(err)
	}
	if err := s.DB.Where("model_name = ?", "gpt-4o").First(&chat).Error; err != nil {
		t.Fatal(err)
	}
	counts := func() [3]int {
		var out [3]int
		for i, table := range []string{"tenant_model_provider", "tenant_model_instance", "tenant_model"} {
			if err := s.Read.QueryRowContext(t.Context(), "SELECT count(*) FROM "+table).Scan(&out[i]); err != nil {
				t.Fatal(err)
			}
		}
		return out
	}
	before := counts()
	empty := func() {
		t.Helper()
		entries, err := os.ReadDir(directory)
		if err != nil || len(entries) != 0 {
			t.Fatal("temporary ASR files survived request", len(entries), err)
		}
		journal.Event("cleanup-readback", map[string]any{"kind": "directory-contents", "path": directory, "empty": true})
	}
	for _, stream := range []bool{false, true} {
		if !stream {
			result, code, err := svc.TranscribeAudio(nil, nil, nil, &model.ID, "model-owner", &encoded, &models.APIConfig{Context: t.Context()}, nil)
			if err != nil || code != common.CodeSuccess || result == nil || result.Text != "spoken" {
				t.Fatal(result, code, err)
			}
		} else {
			var text string
			code, err := svc.TranscribeAudioStream(nil, nil, nil, &model.ID, "model-owner", &encoded, &models.APIConfig{Context: t.Context()}, nil, func(value, reason *string) error {
				if value != nil && *value != "[DONE]" {
					text += *value
				}
				return nil
			})
			if err != nil || code != common.CodeSuccess || text != "spoken" {
				t.Fatal(text, code, err)
			}
		}
		empty()
	}
	// Invalid remote payloads never become local paths and never reach the provider.
	baseline := calls.Load()
	oversized := strings.Repeat("A", 4*((32*1024*1024+2)/3)+4)
	for _, payload := range []*string{nil, stringPointerForAudioTest(""), stringPointerForAudioTest("/etc/hosts"), stringPointerForAudioTest("aGk=\n"), stringPointerForAudioTest("aGk=="), &oversized} {
		if result, code, err := svc.TranscribeAudio(nil, nil, nil, &model.ID, "model-owner", payload, &models.APIConfig{Context: t.Context()}, nil); err == nil || result != nil || code != common.CodeBadRequest {
			t.Fatal("invalid audio payload accepted", code, err)
		}
		empty()
	}
	for _, selection := range []struct{ id, user string }{{model.ID, "model-other"}, {chat.ID, "model-owner"}, {"not-a-model", "model-owner"}} {
		if result, _, err := svc.TranscribeAudio(nil, nil, nil, &selection.id, selection.user, &encoded, &models.APIConfig{Context: t.Context()}, nil); err == nil || result != nil {
			t.Fatal("unauthorized or wrong type audio accepted", err)
		}
		empty()
	}
	if calls.Load() != baseline {
		t.Fatal("rejected audio caused provider I/O")
	}
	want := errors.New("sender stopped")
	if _, err := svc.TranscribeAudioStream(nil, nil, nil, &model.ID, "model-owner", &encoded, &models.APIConfig{Context: t.Context()}, nil, func(*string, *string) error { return want }); !errors.Is(err, want) {
		t.Fatal("sender failure changed", err)
	}
	empty()
	stall.Store(true)
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	result := make(chan error, 1)
	go func() {
		_, _, err := svc.TranscribeAudio(nil, nil, nil, &model.ID, "model-owner", &encoded, &models.APIConfig{Context: ctx}, nil)
		result <- err
	}()
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("ASR did not reach HTTP boundary")
	}
	cancel()
	select {
	case err := <-result:
		if !errors.Is(err, context.Canceled) {
			t.Fatal("ASR cancellation changed", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("ASR did not stop")
	}
	select {
	case <-stopped:
	case <-time.After(5 * time.Second):
		t.Fatal("provider request survived cancellation")
	}
	empty()
	if after := counts(); after != before {
		t.Fatal("ASR changed independently observed SQL", before, after)
	}
}
func stringPointerForAudioTest(s string) *string { return &s }
