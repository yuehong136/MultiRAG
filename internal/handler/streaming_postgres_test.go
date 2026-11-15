package handler_test

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/testsupport"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestChatSessionSSEDeadlineScratchPostgres(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	var calls atomic.Int64
	stopped := make(chan struct{}, 1)
	upstream := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("Authorization") != "Bearer session-key" {
			t.Error("session supplier key changed")
		}
		var body map[string]any
		if json.NewDecoder(r.Body).Decode(&body) != nil {
			t.Error("session body")
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		select {
		case <-time.After(150 * time.Millisecond):
		case <-r.Context().Done():
			return
		}
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"first\"}}]}\n\n")
		w.(http.Flusher).Flush()
		if strings.Contains(fmt.Sprint(body["messages"]), "cancel") {
			select {
			case <-r.Context().Done():
				stopped <- struct{}{}
			case <-time.After(3 * time.Second):
			}
			return
		}
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\" second\"}}]}\n\ndata: [DONE]\n\n")
	}))
	f.enroll(t, "vllm", "session", "session-key", upstream.URL, "session-model", "chat")
	active, owner := "1", "owner"
	if err := dao.DB.Create(&entity.Chat{ID: "session-dialog", TenantID: "owner-tenant", LLMID: "session-model@session@vllm", LLMSetting: entity.JSONMap{}, PromptConfig: entity.JSONMap{}, KBIDs: entity.JSONSlice{}, Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"complete", "cancelled", "failed-persist"} {
		if err := dao.DB.Create(&entity.ChatSession{ID: id, DialogID: "session-dialog", UserID: &owner, Message: json.RawMessage(`{"messages":[]}`), Reference: json.RawMessage(`[]`)}).Error; err != nil {
			t.Fatal(err)
		}
	}
	request := func(ctx context.Context, who, id, message string) *http.Response {
		t.Helper()
		raw, _ := json.Marshal(map[string]any{"conversation_id": id, "messages": []any{map[string]any{"role": "user", "content": message, "id": "message"}}, "stream": true})
		req, _ := http.NewRequestWithContext(ctx, "POST", f.url+"/v1/conversation/completion", bytes.NewReader(raw))
		req.Header.Set("Content-Type", "application/json")
		if who != "" {
			req.Header.Set("Authorization", "Bearer "+f.tokens[who])
		}
		res, err := f.client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		return res
	}
	stored := func(id string) string {
		t.Helper()
		var value string
		if err := f.Read.QueryRow("SELECT message FROM conversation WHERE id=?", id).Scan(&value); err != nil {
			t.Fatal(err)
		}
		return value
	}
	for _, who := range []string{"", "foreign", "inactive", "revoked", "invited", "member"} {
		res := request(t.Context(), who, "complete", "question")
		raw, _ := io.ReadAll(res.Body)
		res.Body.Close()
		if res.StatusCode == 200 || strings.Contains(string(raw), `"data":true`) || calls.Load() != 0 || stored("complete") != `{"messages":[]}` {
			t.Fatal("unauthorized session reached supplier or SQL", who)
		}
	}
	res := request(t.Context(), "owner", "complete", "question")
	completeReader := bufio.NewReader(res.Body)
	first, err := completeReader.ReadString('\n')
	if err != nil || !strings.Contains(first, "first") || !strings.HasPrefix(res.Header.Get("Content-Type"), "text/event-stream") {
		t.Fatal("first stream frame not flushed past write deadline", err)
	}
	rest, _ := io.ReadAll(completeReader)
	res.Body.Close()
	if !strings.Contains(string(rest), `"data":true`) || !strings.Contains(stored("complete"), "first second") || strings.Contains(stored("complete"), "[DONE]") {
		t.Fatal("completion terminal/persisted answer lost")
	}
	ctx, cancel := context.WithCancel(t.Context())
	res = request(ctx, "owner", "cancelled", "cancel")
	reader := bufio.NewReader(res.Body)
	first, err = reader.ReadString('\n')
	if err != nil || !strings.Contains(first, "first") {
		t.Fatal("cancel stream never began", err)
	}
	cancel()
	res.Body.Close()
	select {
	case <-stopped:
	case <-time.After(2 * time.Second):
		t.Fatal("caller cancel did not release supplier")
	}
	if stored("cancelled") != `{"messages":[]}` {
		t.Fatal("canceled session persisted partial success")
	}
	if err := dao.DB.Exec("CREATE TRIGGER session_reject_update BEFORE UPDATE ON conversation FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='controlled commit failure'").Error; err != nil {
		t.Fatal(err)
	}
	res = request(t.Context(), "owner", "failed-persist", "question")
	raw, _ := io.ReadAll(res.Body)
	res.Body.Close()
	if strings.Contains(string(raw), `"data":true`) || stored("failed-persist") != `{"messages":[]}` {
		t.Fatal("SQL failure fabricated completed session")
	}
	if err := dao.DB.Exec("DROP TRIGGER session_reject_update").Error; err != nil {
		t.Fatal(err)
	}
}
