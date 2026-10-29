package handler

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/service"
)

func TestMCPDiscoveryHTTPRejectsInvalidInputBeforeSQL(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, tc := range []struct {
		body      string
		importing bool
	}{
		{`null`, true}, {`{}`, true}, {`{"mcpServers":null}`, true}, {`{"mcpServers":[]}`, true},
		{`{"mcpServers":{},"mcpServers":{}}`, true}, {`{"mcpServers":{},"timeout":10000}`, true},
		{`{"url":"https://example.com","server_type":"stdio"}`, false}, {`{"url":"https://example.com","server_type":"sse","timeout":10000}`, false},
		{`{"url":"https://example.com","server_type":"sse","headers":{"X":1}}`, false},
		{`{"url":"https://example.com","server_type":"sse","headers":{"Authorization":null}}`, false},
		{strings.Repeat(" ", 1<<20) + `{}`, false},
	} {
		recorder := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(recorder)
		c.Request = httptest.NewRequest("POST", "/", strings.NewReader(tc.body))
		c.Set("user", &entity.User{ID: "owner"})
		h := NewMCPHandler(service.NewMCPService())
		if tc.importing {
			h.ImportMCPServers(c)
		} else {
			h.TestMCPServer(c)
		}
		var value map[string]any
		if json.Unmarshal(recorder.Body.Bytes(), &value) != nil || value["code"] != float64(common.CodeArgumentError) {
			t.Fatal("invalid input reached SQL", recorder.Body.String())
		}
	}
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest("POST", "/", strings.NewReader(`{}`))
	NewMCPHandler(nil).TestMCPServer(c)
	var value map[string]any
	if json.Unmarshal(recorder.Body.Bytes(), &value) != nil || value["code"] == float64(common.CodeSuccess) {
		t.Fatal("anonymous discovery accepted")
	}
}
