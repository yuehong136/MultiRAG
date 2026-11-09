package router

import (
	"bytes"
	"encoding/json"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/handler"
	"multirag/internal/service"
)

func TestAgentExecutionRoutesRequireIdentity(t *testing.T) {
	gin.SetMode(gin.TestMode)
	e := gin.New()
	h := handler.NewAgentHandler(service.NewAgentService(), nil)
	RegisterAgentRoutes(e.Group("/api/v1/agents"), h)
	for _, tc := range []struct{ method, path string }{{"POST", "/agents"}, {"GET", "/agents/id"}, {"PUT", "/agents/id"}, {"DELETE", "/agents/id"}, {"POST", "/agents/id/publish"}, {"DELETE", "/agents/id/versions/v"}, {"POST", "/agents/id/run"}, {"DELETE", "/agents/id/run"}, {"GET", "/agents/templates"}, {"GET", "/agents/prompts"}, {"GET", "/agents/tags"}, {"PUT", "/agents/id/tags"}, {"GET", "/agents/id/versions"}, {"GET", "/agents/id/versions/v"}, {"GET", "/agents/id/sessions"}, {"POST", "/agents/id/sessions"}, {"GET", "/agents/id/sessions/s"}, {"DELETE", "/agents/id/sessions"}, {"DELETE", "/agents/id/sessions/s"}, {"GET", "/agents/id/logs/m"}, {"GET", "/agents/id/webhook/logs"}, {"POST", "/agents/chat/completions"}, {"POST", "/agents/rerun"}, {"POST", "/agents/test_db_connection"}} {
		w := httptest.NewRecorder()
		e.ServeHTTP(w, httptest.NewRequest(tc.method, "/api/v1"+tc.path, bytes.NewBufferString(`{}`)))
		var body struct {
			Code common.ErrorCode `json:"code"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil || body.Code != common.CodeUnauthorized {
			t.Fatalf("%s %s: %d %s (%v)", tc.method, tc.path, w.Code, w.Body.String(), err)
		}
	}
}
