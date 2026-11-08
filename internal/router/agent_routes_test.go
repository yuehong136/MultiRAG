package router

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/service"
)

func TestAgentExecutionRoutesRequireIdentity(t *testing.T) {
	gin.SetMode(gin.TestMode)
	e := gin.New()
	h := handler.NewAgentHandler(service.NewAgentService())
	RegisterAgentExecutionRoutes(e.Group("/api/v1"), h)
	for _, tc := range []struct{ method, path string }{{"POST", "/agents"}, {"GET", "/agents/id"}, {"PUT", "/agents/id"}, {"DELETE", "/agents/id"}, {"POST", "/agents/id/publish"}, {"DELETE", "/agents/id/versions/v"}, {"POST", "/agents/id/run"}, {"DELETE", "/agents/id/run"}} {
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

func TestAgentExecutionRejectsAmbiguousBodiesBeforeIO(t *testing.T) {
	gin.SetMode(gin.TestMode)
	e := gin.New()
	e.Use(func(c *gin.Context) { c.Set("user", &entity.User{ID: "owner"}); c.Next() })
	RegisterAgentExecutionRoutes(e.Group("/api/v1"), handler.NewAgentHandler(service.NewAgentService()))
	for _, tc := range []struct{ method, path, body string }{{"POST", "/agents", "null"}, {"PUT", "/agents/id", "null"}, {"POST", "/agents/id/run", `{"session_id":"python-session"}`}, {"POST", "/agents/id/run", `{} {}`}, {"POST", "/agents/id/run", `{"query":"a","query":"b"}`}, {"POST", "/agents/id/run", `{"Query":"a"}`}, {"POST", "/agents/id/run?version=wrong", `{}`}, {"POST", "/agents/id/publish", `{"dsl":{}}`}, {"POST", "/agents", `{"title":"x","dsl":{},"user_id":"other"}`}, {"DELETE", "/agents/id/run", `{"run_id":"x","agent_id":"other"}`}} {
		w := httptest.NewRecorder()
		req := httptest.NewRequest(tc.method, "/api/v1"+tc.path, bytes.NewBufferString(tc.body))
		req.Header.Set("Content-Type", "application/json")
		e.ServeHTTP(w, req)
		var body struct {
			Code common.ErrorCode `json:"code"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil || body.Code != common.CodeArgumentError || w.Code != http.StatusOK {
			t.Fatalf("%s %s %s: %d %s (%v)", tc.method, tc.path, tc.body, w.Code, w.Body.String(), err)
		}
	}
}
