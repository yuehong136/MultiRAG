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
