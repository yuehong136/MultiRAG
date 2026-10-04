package service

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

// Uses an owned PostgreSQL database and an actual HTTP provider. Opt-in has no business DB fallback.
func TestChatSessionScratchPostgres(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_GO_MODELS_DSN")
	if dsn == "" {
		t.Skip("requires an owned chat-session scratch database")
	}
	u, err := url.Parse(dsn)
	if err != nil || !strings.HasPrefix(strings.TrimPrefix(u.Path, "/"), "multirag_go_models_") {
		t.Fatal("scratch DB not owned")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("connect scratch DB failed")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	oldDB := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = oldDB })
	if err := db.AutoMigrate(&entity.Chat{}, &entity.ChatSession{}, &entity.TenantModelProvider{}, &entity.TenantModelInstance{}, &entity.TenantModel{}, &entity.Tenant{}); err != nil {
		t.Fatal(err)
	}
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("independent readback failed")
	}
	defer readback.Close()
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	var requests atomic.Int32
	fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.URL.Path != "/fixture/chat/completions" || r.Header.Get("Authorization") != "Bearer session-fixture-key" {
			t.Errorf("bad endpoint/credential %s", r.URL.Path)
		}
		var body struct {
			Model       string
			Messages    []map[string]string
			Stream      bool
			Thinking    map[string]string
			Temperature float64
			TopP        float64  `json:"top_p"`
			MaxTokens   int      `json:"max_tokens"`
			Stop        []string `json:"stop"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		want := []map[string]string{{"role": "system", "content": "rules"}, {"role": "user", "content": "old"}, {"role": "assistant", "content": "previous"}, {"role": "user", "content": "question"}}
		if !reflect.DeepEqual(body.Messages, want) || body.Model != "kimi-k2.5" || body.Temperature != 0 || body.TopP != 0.8 {
			t.Errorf("history/settings lost %#v", body)
		}
		if body.Thinking["type"] != "disabled" {
			t.Errorf("explicit thinking false lost %#v", body)
		}
		wantMaxTokens, wantStop := 128, []string{"END", "DONE"}
		if body.Stream {
			wantMaxTokens, wantStop = 256, []string{"stored"}
		}
		if body.MaxTokens != wantMaxTokens || !reflect.DeepEqual(body.Stop, wantStop) {
			t.Errorf("JSON generation settings lost: max_tokens=%d stop=%v", body.MaxTokens, body.Stop)
		}
		if body.Stream {
			fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"reason\",\"content\":\"part\"}}]}\n\ndata: {\"choices\":[{\"delta\":{\"content\":\" two\"}}]}\n\ndata: [DONE]\n\n")
		} else {
			fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
		}
	}))
	defer fixture.Close()
	provider := manager.FindProvider("Moonshot")
	provider.URL["fixture"] = fixture.URL + "/fixture"
	// Instance binding constructs a fresh scoped driver rather than mutating this shared driver.
	if err := db.Create(&entity.TenantModelProvider{ID: "session-provider", TenantID: "session-tenant", ProviderName: "Moonshot"}).Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Create(&entity.TenantModelInstance{ID: "session-instance", ProviderID: "session-provider", InstanceName: "fixture", APIKey: "session-fixture-key", Extra: `{"region":"fixture"}`}).Error; err != nil {
		t.Fatal(err)
	}
	active := "1"
	name := "kimi-k2.5@fixture@Moonshot"
	if err := db.Create(&entity.Tenant{ID: "session-tenant", LLMID: name, Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Create(&entity.Chat{ID: "session-dialog", Status: &active, TenantID: "session-tenant", LLMID: name, LLMSetting: entity.JSONMap{"temperature": 0.7, "top_p": 0.8, "thinking": true, "max_tokens": 256, "stop": []string{"stored"}}, PromptConfig: entity.JSONMap{"system": "rules"}, KBIDs: entity.JSONSlice{}}).Error; err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"sync-session", "stream-session", "embedded-session", "failed-session"} {
		if err := db.Create(&entity.ChatSession{ID: id, DialogID: "session-dialog", Message: json.RawMessage(`{"messages":[]}`), Reference: json.RawMessage(`[]`)}).Error; err != nil {
			t.Fatal(err)
		}
	}
	svc := NewChatSessionService()
	svc.modelProviderService.providerManager = manager
	messages := []map[string]interface{}{{"role": "user", "content": "old"}, {"role": "assistant", "content": "previous"}, {"role": "user", "content": "question", "id": "msg"}}
	config := map[string]interface{}{"thinking": false, "temperature": 0.0, "stream": false}
	if err := json.Unmarshal([]byte(`{"max_tokens":128,"stop":["END","DONE"]}`), &config); err != nil {
		t.Fatal(err)
	}
	result, err := svc.Completion(context.Background(), "session-user", "sync-session", messages, "", config, "msg")
	if err != nil || result["answer"] != "answer" {
		t.Fatalf("completion %#v %v", result, err)
	}
	readStored := func(id string) string {
		t.Helper()
		var raw string
		if err := readback.QueryRow(`SELECT message::text FROM t_ai_conversations WHERE id=$1`, id).Scan(&raw); err != nil {
			t.Fatal(err)
		}
		return raw
	}
	var stored struct {
		Messages []map[string]interface{} `json:"messages"`
	}
	if err := json.Unmarshal([]byte(readStored("sync-session")), &stored); err != nil {
		t.Fatal(err)
	}
	if len(stored.Messages) != 4 || stored.Messages[3]["content"] != "answer" || stored.Messages[3]["role"] != "assistant" {
		t.Fatalf("persisted=%#v", stored)
	}
	var frames []string
	streamConfig := map[string]interface{}{"thinking": false, "temperature": 0.0, "stream": true}
	err = svc.CompletionStream(context.Background(), "session-user", "stream-session", messages, "", streamConfig, "msg", func(data string) error { frames = append(frames, data); return nil })
	if err != nil || len(frames) != 4 || !strings.Contains(frames[len(frames)-1], `"data":true`) {
		t.Fatalf("stream %v %v", frames, err)
	}
	if !strings.Contains(frames[0], `"conversation_id":"stream-session"`) || !strings.Contains(frames[0], `"message_id":"msg"`) || !strings.Contains(frames[0], `"reference":[`) || !strings.Contains(frames[0], `"reasoning_content":"reason"`) || !strings.Contains(frames[2], `"answer":"part two"`) || !strings.Contains(readStored("stream-session"), "part two") {
		t.Fatalf("stream/readback=%v", frames)
	}
	before := readStored("embedded-session")
	if _, err := svc.Completion(context.Background(), "session-user", "embedded-session", messages, name, config, "msg"); err != nil {
		t.Fatal(err)
	}
	if readStored("embedded-session") != before {
		t.Fatal("embedded completion persisted unexpectedly")
	}
	if messages[len(messages)-1]["role"] != "user" || len(messages) != 3 {
		t.Fatal("caller input mutated")
	}
	wantErr := errors.New("sender failed")
	before = readStored("failed-session")
	err = svc.CompletionStream(context.Background(), "session-user", "failed-session", messages, "", streamConfig, "msg", func(string) error { return wantErr })
	if !errors.Is(err, wantErr) {
		t.Fatalf("sender error=%v", err)
	}
	if readStored("failed-session") != before {
		t.Fatal("failed stream changed session")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	count := requests.Load()
	if _, err := svc.Completion(ctx, "session-user", "failed-session", messages, "", config, "msg"); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancellation=%v", err)
	}
	if requests.Load() != count {
		t.Fatal("cancelled completion contacted provider")
	}
	if _, err := svc.Completion(context.Background(), "session-user", "failed-session", messages, "kimi-k2.5@fixture@Other", config, "msg"); err == nil {
		t.Fatal("foreign provider silently accepted")
	}
	if err := db.Create(&entity.TenantModel{ID: "session-disabled", ProviderID: "session-provider", InstanceID: "session-instance", ModelName: "kimi-k2.5", ModelType: "chat", Status: "disabled"}).Error; err != nil {
		t.Fatal(err)
	}
	if _, err := svc.Completion(context.Background(), "session-user", "failed-session", messages, "", config, "msg"); err == nil {
		t.Fatal("disabled model accepted")
	}
	if requests.Load() != count {
		t.Fatal("disabled model reached provider")
	}
	// A DB write failure after model success cannot produce a successful terminal frame.
	if err := db.Delete(&entity.TenantModel{}, "id = ?", "session-disabled").Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Exec(`CREATE FUNCTION session_reject_write() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'owned write failure'; END; $$`).Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Exec(`CREATE TRIGGER session_reject_write BEFORE UPDATE ON t_ai_conversations FOR EACH ROW EXECUTE FUNCTION session_reject_write()`).Error; err != nil {
		t.Fatal(err)
	}
	frames = nil
	err = svc.CompletionStream(context.Background(), "session-user", "failed-session", messages, "", streamConfig, "msg", func(data string) error { frames = append(frames, data); return nil })
	if err == nil || !strings.Contains(err.Error(), "owned write failure") || len(frames) == 0 || strings.Contains(frames[len(frames)-1], `"data":true`) || readStored("failed-session") != before {
		t.Fatalf("write failure falsely succeeded: %v %#v", err, frames)
	}
	if err := db.Exec(`DROP TRIGGER session_reject_write ON t_ai_conversations`).Error; err != nil {
		t.Fatal(err)
	}
	// Capability mismatch stays an explicit failure without model-name heuristics.
	provider.Models = append(provider.Models, &entity.Model{Name: "vision-only", ModelTypes: []string{"image2text"}})
	if _, err := svc.modelProviderService.GetChatModel("session-tenant", "vision-only@fixture@Moonshot"); err == nil {
		t.Fatal("unsupported image capability accepted")
	}
	if model, err := svc.modelProviderService.GetChatModel("session-tenant", ""); err != nil || model.ModelConfig.Thinking == nil || !*model.ModelConfig.Thinking {
		t.Fatalf("default model/thinking %v %v", model, err)
	}

	// Verify array content survives provider transport and independent persisted readback.
	imageParts := []interface{}{map[string]interface{}{"type": "text", "text": "describe"}, map[string]interface{}{"type": "image_url", "image_url": map[string]interface{}{"url": "https://example.com/image.png"}}}
	imageHistory := []map[string]interface{}{{"role": "assistant", "content": "previous", "reasoning_content": "old reasoning"}, {"role": "user", "content": imageParts, "id": "image-msg"}}
	var imageCalls atomic.Int32
	imageProvider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		imageCalls.Add(1)
		var body struct{ Messages []models.Message }
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if len(body.Messages) != 3 || body.Messages[0].Content != "rules" || body.Messages[1].ReasoningContent == nil || *body.Messages[1].ReasoningContent != "old reasoning" || !reflect.DeepEqual(body.Messages[2].Content, imageParts) {
			t.Fatalf("image history lost %#v", body)
		}
		fmt.Fprint(w, `{"choices":[{"message":{"content":"image answer","reasoning_content":"image thought"}}]}`)
	}))
	defer imageProvider.Close()
	provider.URL["fixture"] = imageProvider.URL
	result, err = svc.Completion(context.Background(), "session-user", "sync-session", imageHistory, "", config, "image-msg")
	if err != nil || result["answer"] != "image answer" {
		t.Fatalf("image completion %v %v", result, err)
	}
	if err := json.Unmarshal([]byte(readStored("sync-session")), &stored); err != nil {
		t.Fatal(err)
	}
	if len(stored.Messages) != 3 || !reflect.DeepEqual(stored.Messages[1]["content"], imageParts) || stored.Messages[0]["reasoning_content"] != "old reasoning" || stored.Messages[2]["content"] != "image answer" {
		t.Fatalf("image persistence lost %#v", stored)
	}
	before = readStored("sync-session")
	if err := svc.CompletionStream(context.Background(), "session-user", "sync-session", imageHistory, "", streamConfig, "image-msg", func(string) error { t.Fatal("multimodal stream emitted data"); return nil }); err == nil {
		t.Fatal("multimodal session stream accepted")
	}
	if readStored("sync-session") != before || imageCalls.Load() != 1 {
		t.Fatal("rejected stream changed state or contacted provider")
	}
	if _, err := models.NewDummyModel(nil, models.URLSuffix{}).ChatWithMessages("m", nil, nil, nil); err == nil {
		t.Fatal("unsupported model succeeded")
	}
}
