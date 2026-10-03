package handler_test

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/logger"
	"multirag/internal/server"
	"multirag/internal/service"
)

// Actual handler -> tenant binding -> provider HTTP, plus independent SQL readback.
// Identity is a fixture; production auth is covered separately by router tests.
func TestChatSessionLiveHTTPAndSQL(t *testing.T) {
	configPath := os.Getenv("MULTIRAG_CHAT_SESSION_LIVE_CONFIG")
	if configPath == "" {
		t.Skip("requires an owned chat-session scratch database config")
	}
	t.Chdir(filepath.Join("..", ".."))
	if err := logger.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(configPath); err != nil {
		t.Fatal("read owned config failed")
	}
	if !strings.HasPrefix(server.GetConfig().Database.Database, "multirag_chat_session_") {
		t.Fatal("requires owned chat-session DB")
	}
	if err := dao.InitDB(); err != nil {
		t.Fatal("initialize scratch DB failed")
	}
	pool, err := dao.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	dsn, err := dao.PostgresDSN(server.GetConfig().Database)
	if err != nil {
		t.Fatal(err)
	}
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("independent readback failed")
	}
	defer readback.Close()
	started, cancelled := make(chan struct{}, 1), make(chan struct{}, 1)
	var calls atomic.Int32
	fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("Authorization") != "Bearer session-fixture-key" || r.URL.Path != "/chat/completions" {
			t.Errorf("credential/URL mismatch %s", r.URL.Path)
		}
		var body struct {
			Messages []map[string]string
			Stream   bool
			Thinking map[string]string
			Model    string
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if len(body.Messages) != 4 || body.Messages[0]["role"] != "system" || body.Messages[0]["content"] != "rules" || body.Messages[2]["role"] != "assistant" || body.Messages[2]["content"] != "previous" || body.Model != "kimi-k2.5" {
			t.Errorf("roles/history/model lost %#v", body)
		}
		message := body.Messages[len(body.Messages)-1]["content"]
		wantThinking := "enabled"
		if message == "off" {
			wantThinking = "disabled"
		}
		if body.Thinking["type"] != wantThinking {
			t.Errorf("thinking default/false lost %#v", body)
		}
		if body.Stream {
			w.Header().Set("Content-Type", "text/event-stream")
			if message == "cancel" {
				fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n")
				w.(http.Flusher).Flush()
				started <- struct{}{}
				<-r.Context().Done()
				cancelled <- struct{}{}
				return
			}
			if message == "error" {
				fmt.Fprint(w, "data: {\"error\":{\"message\":\"secret provider credential\"}}\n\n")
				return
			}
			fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"reason\",\"content\":\"part\"}}]}\n\ndata: {\"choices\":[{\"delta\":{\"content\":\" two\"}}]}\n\ndata: [DONE]\n\n")
		} else {
			fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
		}
	}))
	defer fixture.Close()
	provider := dao.GetModelProviderManager().FindProvider("Moonshot")
	oldURL := provider.URL["default"]
	provider.URL["default"] = fixture.URL
	t.Cleanup(func() { provider.URL["default"] = oldURL })
	if err := dao.DB.Create(&entity.TenantModelProvider{ID: "session-provider", TenantID: "session-tenant", ProviderName: "Moonshot"}).Error; err != nil {
		t.Fatal(err)
	}
	if err := dao.DB.Create(&entity.TenantModelInstance{ID: "session-instance", ProviderID: "session-provider", InstanceName: "default", APIKey: "session-fixture-key", Extra: `{}`}).Error; err != nil {
		t.Fatal(err)
	}
	active := "1"
	if err := dao.DB.Create(&entity.Chat{ID: "session-dialog", TenantID: "session-tenant", LLMID: "kimi-k2.5@default@Moonshot", LLMSetting: entity.JSONMap{}, PromptConfig: entity.JSONMap{"system": "rules"}, KBIDs: entity.JSONSlice{}, Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"default", "off", "stream", "error", "cancel"} {
		if err := dao.DB.Create(&entity.ChatSession{ID: id, DialogID: "session-dialog", Message: json.RawMessage(`{"messages":[]}`), Reference: json.RawMessage(`[]`)}).Error; err != nil {
			t.Fatal(err)
		}
	}
	h := handler.NewChatSessionHandler(service.NewChatSessionService(), nil)
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	router.Use(func(c *gin.Context) {
		if c.GetHeader("Authorization") == "Bearer session-fixture-identity" {
			c.Set("user", &entity.User{ID: "session-user"})
		}
		c.Next()
	})
	router.POST("/v1/conversation/completion", h.Completion)
	api := httptest.NewServer(router)
	defer api.Close()
	bodyFor := func(id, message string, flags map[string]interface{}) []byte {
		body := map[string]interface{}{"conversation_id": id, "messages": []map[string]string{{"role": "system", "content": "untrusted"}, {"role": "assistant", "content": "prologue"}, {"role": "user", "content": "old"}, {"role": "assistant", "content": "previous"}, {"role": "user", "content": message, "id": "msg"}}}
		for k, v := range flags {
			body[k] = v
		}
		data, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		return data
	}
	send := func(id, message string, flags map[string]interface{}, authenticated bool) (int, string) {
		t.Helper()
		req, err := http.NewRequest(http.MethodPost, api.URL+"/v1/conversation/completion", bytes.NewReader(bodyFor(id, message, flags)))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		if authenticated {
			req.Header.Set("Authorization", "Bearer session-fixture-identity")
		}
		resp, err := api.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		data, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatal(err)
		}
		return resp.StatusCode, string(data)
	}
	count := calls.Load()
	_, denied := send("default", "q", nil, false)
	var denial struct{ Code int }
	json.Unmarshal([]byte(denied), &denial)
	if denial.Code == 0 || calls.Load() != count {
		t.Fatal("unauthorized completion reached provider")
	}
	for _, test := range []struct {
		id, msg string
		flags   map[string]interface{}
	}{{"default", "default", nil}, {"off", "off", map[string]interface{}{"stream": false, "thinking": false}}} {
		status, body := send(test.id, test.msg, test.flags, true)
		var payload struct {
			Code int
			Data struct{ Answer string }
		}
		if err := json.Unmarshal([]byte(body), &payload); err != nil || status != http.StatusOK || payload.Code != 0 || payload.Data.Answer != "answer" {
			t.Fatalf("JSON completion %d %s", status, body)
		}
	}
	status, body := send("stream", "stream", map[string]interface{}{"stream": true}, true)
	if status != http.StatusOK || !strings.Contains(body, `"reasoning_content":"reason"`) || !strings.Contains(body, `"answer":"part two"`) || !strings.Contains(body, `"data":true`) {
		t.Fatalf("SSE %d %s", status, body)
	}
	var stored string
	if err := readback.QueryRow(`SELECT message::text FROM t_ai_conversations WHERE id='stream'`).Scan(&stored); err != nil || !strings.Contains(stored, "part two") {
		t.Fatal("independent persisted answer readback failed")
	}
	_, body = send("error", "error", map[string]interface{}{"stream": true}, true)
	if strings.Contains(body, "secret provider") || strings.Contains(body, `"data":true`) || !strings.Contains(body, "Model stream failed") {
		t.Fatalf("stream error %s", body)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, api.URL+"/v1/conversation/completion", bytes.NewReader(bodyFor("cancel", "cancel", map[string]interface{}{"stream": true})))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer session-fixture-identity")
	req.Header.Set("Content-Type", "application/json")
	response, err := api.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-started:
	case <-time.After(3 * time.Second):
		t.Fatal("cancel stream did not start")
	}
	cancel()
	response.Body.Close()
	select {
	case <-cancelled:
	case <-time.After(3 * time.Second):
		t.Fatal("downstream request did not cancel")
	}
	if err := readback.QueryRow(`SELECT message::text FROM t_ai_conversations WHERE id='cancel'`).Scan(&stored); err != nil || strings.Contains(stored, "partial") {
		t.Fatal("cancelled stream persisted partial success")
	}
}
