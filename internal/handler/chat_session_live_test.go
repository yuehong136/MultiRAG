package handler_test

import (
	"encoding/json"
	"fmt"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/testsupport"
	"net/http"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
)

func TestChatSessionLiveHTTPAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	var calls atomic.Int64
	var mode atomic.Value
	mode.Store("default")
	history := []any{map[string]any{"role": "user", "content": "old"}, map[string]any{"role": "assistant", "content": "previous"}, map[string]any{"role": "user", "content": []any{map[string]any{"type": "text", "text": "question"}, map[string]any{"type": "image_url", "image_url": map[string]any{"url": "https://example.invalid/a.png"}}}, "id": "message"}}
	upstream := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("Authorization") != "Bearer session-key" || !strings.HasSuffix(r.URL.Path, "/chat/completions") {
			t.Error("session instance endpoint or key changed")
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		want := []any{map[string]any{"role": "system", "content": "rules"}}
		for _, row := range history {
			m := row.(map[string]any)
			want = append(want, map[string]any{"role": m["role"], "content": m["content"]})
		}
		if body["model"] != "kimi-k2.5" || !reflect.DeepEqual(body["messages"], want) {
			t.Error("dialog system, full history, or image changed", body)
		}
		if mode.Load() == "off" {
			if !reflect.DeepEqual(body["thinking"], map[string]any{"type": "disabled"}) {
				t.Error("explicit false thinking lost")
			}
			if body["temperature"] != float64(0) || body["top_p"] != float64(0) {
				t.Error("explicit zero request options lost")
			}
		} else if body["thinking"] != nil {
			t.Error("omitted thinking became explicit")
		}
		if mode.Load() == "failure" {
			w.WriteHeader(503)
			fmt.Fprint(w, "private-provider-body")
			return
		}
		if mode.Load() == "conflict" {
			if err := dao.DB.Model(&entity.ChatSession{}).Where("id=?", "conflict").Update("message", `{"messages":[{"role":"assistant","content":"concurrent"}]}`).Error; err != nil {
				t.Error(err)
			}
		}
		fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
	}))
	f.enroll(t, "Moonshot", "session", "session-key", upstream.URL, "kimi-k2.5", "chat")
	active, owner := "1", "owner"
	if err := dao.DB.Create(&entity.Chat{ID: "solo-dialog", TenantID: "owner-tenant", LLMID: "kimi-k2.5@session@Moonshot", LLMSetting: entity.JSONMap{}, PromptConfig: entity.JSONMap{"system": "rules"}, KBIDs: entity.JSONSlice{}, Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"default", "off", "failure", "custom", "conflict"} {
		if err := dao.DB.Create(&entity.ChatSession{ID: id, DialogID: "solo-dialog", UserID: &owner, Message: json.RawMessage(`{"messages":[]}`), Reference: json.RawMessage(`[]`)}).Error; err != nil {
			t.Fatal(err)
		}
	}
	body := func(id string) map[string]any {
		messages := []any{map[string]any{"role": "system", "content": "untrusted"}, map[string]any{"role": "assistant", "content": "initial"}}
		return map[string]any{"conversation_id": id, "messages": append(messages, history...)}
	}
	stored := func(id string) string {
		t.Helper()
		var value string
		if err := f.Read.QueryRow("SELECT message FROM conversation WHERE id=?", id).Scan(&value); err != nil {
			t.Fatal(err)
		}
		return value
	}
	before := f.snapshot(t)
	for _, actor := range []string{"", "foreign", "inactive", "revoked", "invited", "member", "disabledtenant"} {
		_, denied, _ := f.request(t, actor, "POST", "/v1/conversation/completion", body("default"))
		if providerEnvelopeError(denied) == nil || calls.Load() != 0 || f.snapshot(t) != before {
			t.Fatal("denied solo chat changed SQL or reached supplier", actor)
		}
	}
	for _, id := range []string{"default", "off"} {
		mode.Store(id)
		payload := body(id)
		if id == "off" {
			payload["stream"], payload["thinking"] = false, false
			payload["temperature"], payload["top_p"] = 0, 0
		}
		result := f.requireSuccess(t, "owner", "POST", "/v1/conversation/completion", payload)
		if result["data"].(map[string]any)["answer"] != "answer" || !strings.Contains(stored(id), "image_url") || !strings.Contains(stored(id), "answer") || strings.Contains(stored(id), "untrusted") || strings.Contains(stored(id), "initial") {
			t.Fatal("solo response or persisted canonical history changed", result)
		}
	}
	mode.Store("default")
	before = f.snapshot(t)
	count := calls.Load()
	custom := body("custom")
	custom["llm_id"] = "missing@session@Moonshot"
	_, denied, _ := f.request(t, "owner", "POST", "/v1/conversation/completion", custom)
	if providerEnvelopeError(denied) == nil || calls.Load() != count || f.snapshot(t) != before {
		t.Fatal("unconfigured custom model reached supplier or changed SQL")
	}
	custom["llm_id"] = "kimi-k2.5@session@Moonshot"
	f.requireSuccess(t, "owner", "POST", "/v1/conversation/completion", custom)
	if f.snapshot(t) != before {
		t.Fatal("embedded custom-model completion persisted session")
	}
	mode.Store("failure")
	_, failed, raw := f.request(t, "owner", "POST", "/v1/conversation/completion", body("failure"))
	if providerEnvelopeError(failed) == nil || strings.Contains(raw, "private-provider-body") || stored("failure") != `{"messages":[]}` {
		t.Fatal("provider failure leaked or persisted success")
	}
	mode.Store("conflict")
	_, failed, _ = f.request(t, "owner", "POST", "/v1/conversation/completion", body("conflict"))
	if providerEnvelopeError(failed) == nil || !strings.Contains(stored("conflict"), "concurrent") || strings.Contains(stored("conflict"), "answer") {
		t.Fatal("stale conversation overwrote concurrent update")
	}
}
