package handler

import (
	"encoding/json"
	"github.com/gin-gonic/gin"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestModelChatRequestBooleanPresence(t *testing.T) {
	for _, flags := range []string{"", `,"stream":false,"thinking":false`, `,"stream":true,"thinking":true`} {
		var req ChatToModelRequest
		if err := json.Unmarshal([]byte(`{"provider_name":"Moonshot","instance_name":"default","model_name":"kimi-k2.5","message":"hello"`+flags+`}`), &req); err != nil {
			t.Fatal(err)
		}
		if flags == "" {
			if req.Stream != nil || req.Thinking != nil {
				t.Fatal("omitted flags lost")
			}
		} else if req.Stream == nil || req.Thinking == nil || *req.Stream != strings.Contains(flags, "true") || *req.Thinking != *req.Stream {
			t.Fatalf("flags = %#v", req)
		}
	}
}

func TestModelChatRejectsInvalidBodyBeforeService(t *testing.T) {
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	h := NewProviderHandler(nil, nil)
	router.POST("/chat/completions", h.ChatToModel)
	for _, body := range []string{`{`, `{}`, `{"provider_name":"P","instance_name":"I","model_name":"M"}`, `{"provider_name":" ","instance_name":"I","model_name":"M","message":"q"}`, `{"provider_name":"P","instance_name":"I","model_name":"M","message":"q","stream":"false"}`} {
		w := httptest.NewRecorder()
		router.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/chat/completions", strings.NewReader(body)))
		if w.Code != http.StatusBadRequest {
			t.Fatalf("body %s returned %d", body, w.Code)
		}
	}
	w := httptest.NewRecorder()
	router.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/chat/completions", strings.NewReader(`{"provider_name":"P","instance_name":"I","model_name":"M","message":"q"}`)))
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("authentication = %d", w.Code)
	}
}

func TestModelChatMessageContracts(t *testing.T) {
	for _, test := range []struct {
		body  string
		valid bool
		count int
	}{
		{`{"message":"legacy"}`, true, 1},
		{`{"messages":[{"role":"system","content":"rules"},{"role":"assistant","content":"old","reasoning_content":"thought"},{"role":"user","content":"next"}],"stream":true}`, true, 3},
		{`{"messages":[{"role":"user","content":[{"type":"text","text":"describe"},{"type":"image_url","image_url":{"url":"https://example.com/a.png"}}]}]}`, true, 1},
		{`{"messages":[]}`, false, 0}, {`{"messages":[{}]}`, false, 0},
		{`{"message":"legacy","messages":[]}`, false, 0},
		{`{"messages":[{"role":"user","content":42}]}`, false, 0},
		{`{"messages":[{"role":"user","content":{"text":"x"}}]}`, false, 0},
		{`{"messages":[{"role":"user","content":[{"type":"text","text":"x"}]}],"stream":true}`, false, 0},
	} {
		var req ChatToModelRequest
		if err := json.Unmarshal([]byte(test.body), &req); err != nil {
			t.Fatal(err)
		}
		messages, err := req.chatMessages()
		if (err == nil) != test.valid || (test.valid && len(messages) != test.count) {
			t.Fatalf("%s: %#v %v", test.body, messages, err)
		}
		if test.valid && test.count == 3 && (messages[1].ReasoningContent == nil || *messages[1].ReasoningContent != "thought") {
			t.Fatal("reasoning history lost")
		}
	}
}
