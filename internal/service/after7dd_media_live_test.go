package service

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/testsupport"
)

// This case requires a pre-registered, empty database owned by the caller. It
// never creates or drops a database or borrows application configuration.
func TestAfter7ddXiaomiServiceMedia(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_AFTER7DD_MEDIA_DSN")
	if dsn == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned media database required")
		}
		t.Skip("requires owned media database")
	}
	endpoint, err := url.Parse(dsn)
	if err != nil || endpoint.Hostname() != "127.0.0.1" || !strings.HasPrefix(strings.TrimPrefix(endpoint.Path, "/"), "multirag_test_after7dd_") {
		t.Fatal("owned loopback scratch database required")
	}
	journal := testsupport.NewCase(t)
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("owned media database unavailable")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	read, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("readback unavailable")
	}
	t.Cleanup(func() { read.Close() })
	var actual string
	if read.QueryRow("SELECT current_database()").Scan(&actual) != nil || actual != strings.TrimPrefix(endpoint.Path, "/") {
		t.Fatal("database identity mismatch")
	}
	journal.Event("database-identity-readback", map[string]any{"database": actual, "host": endpoint.Hostname(), "port": endpoint.Port()})
	schema := []any{&entity.UserTenant{}, &entity.TenantModelProvider{}, &entity.TenantModelInstance{}, &entity.TenantModel{}}
	for _, model := range schema {
		if db.Migrator().HasTable(model) {
			t.Fatal("media fixture requires fresh schema")
		}
	}
	journal.Event("reserve-media-schema", map[string]any{"database": actual, "tables": []string{"t_ai_user_tenants", "tenant_model_provider", "tenant_model_instance", "tenant_model"}})
	if err := db.AutoMigrate(schema...); err != nil {
		t.Fatal("media schema unavailable")
	}
	oldDB := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = oldDB })
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	svc := NewModelProviderServiceWithCatalog(manager, nil)
	const owner = "after7dd-media-owner"
	const providerID = "after7dd-media-provider"
	const instanceID = "after7dd-media-instance"
	const instanceName = "controlled"
	const asrName = "mimo-v2.5-asr"
	const ttsName = "mimo-v2.5-tts"
	var calls atomic.Int64
	var expectedFormat atomic.Value
	expectedFormat.Store("mp3")
	var mode atomic.Int32 // 1=rejected finish, 2=stream waits for sender cancellation
	closed := make(chan struct{}, 8)
	server := afteraabModelBindingServer(t, journal, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Method != "POST" || r.URL.Path != "/v1/chat/completions" || r.Header.Get("api-key") != "stored-media-key" || r.Header.Get("Authorization") != "" {
			t.Error("SQL-selected endpoint/auth lost")
			w.WriteHeader(400)
			return
		}
		var body map[string]any
		if json.NewDecoder(r.Body).Decode(&body) != nil {
			t.Error("invalid request JSON")
			w.WriteHeader(400)
			return
		}
		name, _ := body["model"].(string)
		if name != asrName && name != ttsName {
			t.Error("wrong SQL model")
			w.WriteHeader(400)
			return
		}
		messages, ok := body["messages"].([]any)
		if !ok || len(messages) != 1 {
			t.Error("missing media input")
			w.WriteHeader(400)
			return
		}
		message, ok := messages[0].(map[string]any)
		if !ok {
			t.Error("invalid media message")
			w.WriteHeader(400)
			return
		}
		payload := map[string]any{"content": "transcribed"}
		if name == asrName {
			content, ok := message["content"].([]any)
			if !ok || len(content) != 1 {
				t.Error("ASR content shape")
				w.WriteHeader(400)
				return
			}
			audio, ok := content[0].(map[string]any)
			if !ok {
				t.Error("ASR content item")
				w.WriteHeader(400)
				return
			}
			encoded, ok := audio["input_audio"].(map[string]any)
			if !ok || encoded["data"] != "data:audio/mpeg;base64,YWJj" {
				t.Error("ASR bytes/MIME changed")
				w.WriteHeader(400)
				return
			}
			options, ok := body["asr_options"].(map[string]any)
			if !ok || options["language"] != "en" {
				t.Error("ASR options changed")
				w.WriteHeader(400)
				return
			}
		} else {
			if message["role"] != "assistant" || message["content"] != "say" {
				t.Error("TTS input changed")
				w.WriteHeader(400)
				return
			}
			audio, ok := body["audio"].(map[string]any)
			if !ok || audio["format"] != expectedFormat.Load().(string) {
				t.Error("TTS format changed")
				w.WriteHeader(400)
				return
			}
			payload = map[string]any{"audio": map[string]any{"data": "YWJj"}}
		}
		finish := "stop"
		if mode.Load() == 1 {
			finish = "content_filter"
		}
		stream, _ := body["stream"].(bool)
		if stream {
			w.Header().Set("Content-Type", "text/event-stream")
			raw, _ := json.Marshal(map[string]any{"choices": []any{map[string]any{"delta": payload, "finish_reason": finish}}})
			fmt.Fprintf(w, "data: %s\n\n", raw)
			w.(http.Flusher).Flush()
			if mode.Load() == 2 {
				<-r.Context().Done()
				closed <- struct{}{}
				return
			}
			fmt.Fprint(w, "data: [DONE]\n\n")
		} else {
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode(map[string]any{"choices": []any{map[string]any{"message": payload, "finish_reason": finish}}})
		}
	}))
	extra, err := encodeProviderInstanceExtra("default", server.URL)
	if err != nil {
		t.Fatal(err)
	}
	active := "1"
	journal.Event("reserve-media-SQL-identities", map[string]any{"database": actual, "membership": owner, "provider": providerID, "instance": instanceID, "model_names": []string{asrName, ttsName}})
	t.Cleanup(func() {
		journal.Event("reserve-media-SQL-delete", map[string]any{"database": actual, "membership": owner, "provider": providerID, "instance": instanceID})
		clean := true
		for _, item := range []struct{ table, id string }{{"tenant_model_instance", instanceID}, {"tenant_model_provider", providerID}, {"t_ai_user_tenants", owner}} {
			if err := db.Table(item.table).Where("id = ?", item.id).Delete(map[string]any{}).Error; err != nil {
				clean = false
				t.Error("fixture delete failed")
			}
			var count int
			if err := read.QueryRow("SELECT count(*) FROM "+item.table+" WHERE id=$1", item.id).Scan(&count); err != nil || count != 0 {
				clean = false
				t.Error("fixture survived SQL cleanup")
			}
		}
		journal.Event("media-SQL-cleanup-readback", map[string]any{"owned_rows_absent": clean, "database_retained_for_owner_drop": actual})
	})
	if err := db.Create(&entity.UserTenant{ID: owner, UserID: owner, TenantID: owner, Role: "owner", InvitedBy: owner, Status: &active}).Error; err != nil {
		t.Fatal("membership insert failed")
	}
	if err := db.Create(&entity.TenantModelProvider{ID: providerID, ProviderName: "Xiaomi", TenantID: owner}).Error; err != nil {
		t.Fatal("provider insert failed")
	}
	if err := db.Create(&entity.TenantModelInstance{ID: instanceID, InstanceName: instanceName, ProviderID: providerID, APIKey: "stored-media-key", Status: "active", Extra: extra}).Error; err != nil {
		t.Fatal("instance insert failed")
	}
	// The catalog provides the model capabilities; tenant_model has no override.
	snapshot := func() string {
		t.Helper()
		var value string
		err := read.QueryRow(`SELECT json_build_array((SELECT json_agg(row(id,user_id,tenant_id,role,status)) FROM t_ai_user_tenants),(SELECT json_agg(row(id,provider_name,tenant_id)) FROM tenant_model_provider),(SELECT json_agg(row(id,instance_name,provider_id,status,extra)) FROM tenant_model_instance),(SELECT count(*) FROM tenant_model))::text`).Scan(&value)
		if err != nil {
			t.Fatal("independent SQL readback failed")
		}
		return value
	}
	before := snapshot()

	// Deliberately supplied caller credentials/region must never override SQL binding.
	hostileKey, hostileRegion := "caller-key", "caller-region"
	api := &models.APIConfig{APIKey: &hostileKey, Region: &hostileRegion, Context: t.Context()}
	asr := &models.ASRConfig{Format: "mp3", Language: []string{"en"}}
	result, code, err := svc.TranscribeAudio("Xiaomi", instanceName, asrName, owner, []byte("abc"), api, asr)
	if err != nil || code != common.CodeSuccess || result == nil || result.Text == nil || *result.Text != "transcribed" {
		t.Fatal("ASR service failed", code, err)
	}
	n := 0
	code, err = svc.TranscribeAudioStream("Xiaomi", instanceName, asrName, owner, []byte("abc"), api, asr, func(result *models.ASRResponse) error {
		n++
		if result.Text == nil || *result.Text != "transcribed" {
			t.Error("streamed ASR changed")
		}
		return nil
	})
	if err != nil || code != common.CodeSuccess || n != 1 {
		t.Fatal("ASR stream service failed", code, err, n)
	}
	for _, cfg := range []*models.TTSConfig{nil, {Params: map[string]any{"voice": "voice"}}, {Format: "wav", Params: map[string]any{"format": "mp3", "voice": "voice"}}} {
		format, e := models.SpeechFormat(cfg)
		expectedFormat.Store(format)
		if e != nil {
			t.Fatal(e)
		}
		speech, code, e := svc.AudioSpeech("Xiaomi", instanceName, ttsName, owner, "say", api, cfg)
		if e != nil || code != common.CodeSuccess || speech == nil || string(speech.Audio) != "abc" || speech.Format != format {
			t.Fatal("TTS service format contract", code, e)
		}
		n = 0
		code, e = svc.AudioSpeechStream("Xiaomi", instanceName, ttsName, owner, "say", api, cfg, func(result *models.TTSResponse) error {
			n++
			if string(result.Audio) != "abc" || result.Format != format {
				t.Error("streamed TTS contract changed")
			}
			return nil
		})
		if e != nil || code != common.CodeSuccess || n != 1 {
			t.Fatal("TTS stream service failed", code, e, n)
		}
	}
	if snapshot() != before {
		t.Fatal("media reads changed SQL")
	}
	// Permission/capability denials cannot contact the provider.
	count := calls.Load()
	if _, code, err = svc.AudioSpeech("Xiaomi", instanceName, asrName, owner, "say", api, nil); err == nil || code == common.CodeSuccess {
		t.Fatal("ASR model allowed TTS")
	}
	if _, code, err = svc.TranscribeAudio("Xiaomi", instanceName, asrName, "other", []byte("abc"), api, asr); err == nil || code == common.CodeSuccess {
		t.Fatal("non-owner allowed ASR")
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	cancelled := *api
	cancelled.Context = ctx
	if _, _, err = svc.AudioSpeech("Xiaomi", instanceName, ttsName, owner, "say", &cancelled, nil); !errors.Is(err, context.Canceled) {
		t.Fatal("cancelled caller not preserved", err)
	}
	if calls.Load() != count || snapshot() != before {
		t.Fatal("denials caused remote IO or SQL mutation")
	}
	expectedFormat.Store("mp3")
	// A rejected finish is an error and must never reach the sender.
	mode.Store(1)
	n = 0
	code, err = svc.AudioSpeechStream("Xiaomi", instanceName, ttsName, owner, "say", api, nil, func(*models.TTSResponse) error { n++; return nil })
	if err == nil || code == common.CodeSuccess || n != 0 {
		t.Fatal("rejected finish delivered success", code, err, n)
	}
	// Callback failure propagates unchanged and response closure cancels supplier.
	mode.Store(2)
	sentinel := errors.New("controlled sender stop")
	n = 0
	code, err = svc.TranscribeAudioStream("Xiaomi", instanceName, asrName, owner, []byte("abc"), api, asr, func(*models.ASRResponse) error { n++; return sentinel })
	if !errors.Is(err, sentinel) || code == common.CodeSuccess || n != 1 {
		t.Fatal("sender failure lost", code, err, n)
	}
	select {
	case <-closed:
	case <-time.After(5 * time.Second):
		t.Fatal("supplier request survived sender failure")
	}
	if snapshot() != before {
		t.Fatal("failed media call changed SQL")
	}
	journal.Event("media-consumer-complete", map[string]any{"four_service_entries": true, "http_calls": calls.Load(), "independent_SQL_unchanged": true, "sender_failure_drained": true})
}
