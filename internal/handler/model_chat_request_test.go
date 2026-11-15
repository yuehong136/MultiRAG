package handler

import (
	"encoding/json"
	"github.com/gin-gonic/gin"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
)

func TestCanonicalModelChatRequestShape(t *testing.T) {
	for _, flag := range []bool{false, true} {
		raw := `{"model_id":"0123456789abcdef0123456789abcdef","messages":[{"role":"user","content":[{"type":"text","text":"describe"},{"type":"image_url","image_url":{"url":"https://example.invalid/a.png"}}]}]}`
		var request ChatToModelRequest
		if err := json.Unmarshal([]byte(raw), &request); err != nil {
			t.Fatal(err)
		}
		if request.ModelID == nil || request.Stream || request.Thinking || len(request.Messages) != 1 {
			t.Fatal("canonical request defaults lost")
		}
		request.Stream = flag
		encoded, err := json.Marshal(request)
		if err != nil {
			t.Fatal(err)
		}
		var again ChatToModelRequest
		if err := json.Unmarshal(encoded, &again); err != nil || again.Stream != flag || !reflect.DeepEqual(again.Messages, request.Messages) {
			t.Fatal("messages changed during wire roundtrip", err)
		}
	}
}
func TestCanonicalModelChatBinderRejectsInvalidTypes(t *testing.T) {
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	handler := NewProviderHandler(nil, nil)
	router.POST("/chat", handler.ChatToModel)
	for _, raw := range []string{`{`, `{"model_id":17}`, `{"stream":"false"}`, `{"messages":"message"}`} {
		response := httptest.NewRecorder()
		router.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/chat", strings.NewReader(raw)))
		var body struct {
			Code int `json:"code"`
		}
		if json.Unmarshal(response.Body.Bytes(), &body) != nil || body.Code == 0 {
			t.Fatal("invalid body accepted", raw, response.Code)
		}
	}
}
