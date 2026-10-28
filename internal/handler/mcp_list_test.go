package handler

import (
	"encoding/json"
	"github.com/gin-gonic/gin"
	"multirag/internal/entity"
	"net/http/httptest"
	"net/url"
	"testing"
)

func TestMCPListStrictQuery(t *testing.T) {
	for _, raw := range []string{"page=-1", "page=NaN", "page=", "page=1&page=2", "page_size=101", "page_size=-1", "desc=yes", "desc=", "orderby=name%3BDROP", "mcp_ids=", "mcp_ids=null", "mcp_ids=[1]", "mcp_ids=a,a", "mcp_ids=[]&mcp_id=a", "mcp_ids=a,,b", "mcp_ids=%20a", "tenant_id=", "page=9223372036854775807&page_size=100"} {
		q, _ := url.ParseQuery(raw)
		if _, _, err := parseMCPListQuery(q); err == nil {
			t.Errorf("accepted malformed query %s", raw)
		}
	}
	for _, raw := range []string{"", "mcp_ids=[]", "mcp_ids=a,b&mcp_ids=c", "mcp_id=a", "page=0&page_size=0", "page=2&page_size=100&desc=false&orderby=name"} {
		q, _ := url.ParseQuery(raw)
		opts, _, err := parseMCPListQuery(q)
		if err != nil {
			t.Errorf("refused valid query %s", raw)
		}
		if raw == "" && opts.IDs != nil {
			t.Error("absent filter broadened")
		}
		if raw == "mcp_ids=[]" && (opts.IDs == nil || len(opts.IDs) != 0) {
			t.Error("empty filter lost")
		}
	}
}

func TestMCPListMalformedEncodingDoesNotBroaden(t *testing.T) {
	for _, rawQuery := range []string{"mcp_ids=%ZZ", "orderby=name;DROP"} {
		recorder := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(recorder)
		c.Request = httptest.NewRequest("GET", "/api/v1/mcp/servers", nil)
		c.Request.URL.RawQuery = rawQuery
		c.Set("user", &entity.User{ID: "owner"})
		(&MCPHandler{}).ListMCPServers(c)
		var payload map[string]any
		if json.Unmarshal(recorder.Body.Bytes(), &payload) != nil || payload["code"] == float64(0) {
			t.Fatal("invalid encoding broadened filter", rawQuery)
		}
	}
}
