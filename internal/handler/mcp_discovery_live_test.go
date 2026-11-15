package handler

import (
	"fmt"
	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/service"
	"strings"
	"testing"
)

func TestMCPManagementHTTPAndSQL(t *testing.T) {
	f := newCanonicalHTTPFixture(t)
	if err := f.DB.AutoMigrate(&entity.MCPServer{}); err != nil {
		t.Fatal(err)
	}
	row := entity.MCPServer{ID: f.Prefix + "mcp", TenantID: f.ids["owner"], Name: "owned", URL: "http://127.0.0.1:9/mcp", ServerType: "sse", Headers: entity.JSONMap{"Authorization": "hidden-auth"}, Variables: entity.JSONMap{}}
	if err := f.DB.Create(&row).Error; err != nil {
		t.Fatal(err)
	}
	h := NewMCPHandler(service.NewMCPService())
	app := f.app()
	app.POST("/mcp/servers/import", h.ImportMCPServers)
	app.POST("/mcp/servers/:mcp_id/test", h.TestMCPServer)
	app.GET("/mcp/servers", h.ListMCPServers)
	app.DELETE("/mcp/servers/:mcp_id", h.DeleteMCPServer)
	url := f.serve(t, app)
	before := func() string {
		var value string
		if err := f.Read.QueryRow("SELECT concat(id,tenant_id,name,url,headers,variables) FROM mcp_server WHERE id=?", row.ID).Scan(&value); err != nil {
			t.Fatal(err)
		}
		return value
	}
	snapshot := before()
	for _, actor := range []string{"owner", "member", "foreign"} {
		_, result, _, _ := f.request(t, url, actor, "GET", "/mcp/servers", nil)
		if result["code"] != float64(common.CodeSuccess) {
			t.Fatal(result)
		}
		raw := result["data"].(map[string]any)
		servers := raw["mcp_servers"].([]any)
		want := 0
		if actor == "owner" {
			want = 1
		}
		if len(servers) != want {
			t.Fatal("MCP tenant list scope", actor, result)
		}
	}
	for _, actor := range []string{"member", "foreign"} {
		_, result, _, _ := f.request(t, url, actor, "DELETE", "/mcp/servers/"+row.ID, nil)
		if result["code"] == float64(0) || before() != snapshot {
			t.Fatal("foreign MCP deletion", actor, result)
		}
	}
	for _, body := range []map[string]any{{}, {"mcpServers": map[string]any{}}, {"mcpServers": map[string]any{"bad": map[string]any{"type": "sse", "url": "http://127.0.0.1:9/mcp", "headers": map[string]any{"Authorization": nil}}}}, {"mcpServers": map[string]any{"blocked": map[string]any{"type": "sse", "url": "http://127.0.0.1:9/mcp"}}}, {"timeout": 10000, "mcpServers": map[string]any{"x": map[string]any{"type": "sse", "url": "http://127.0.0.1:9/mcp"}}}} {
		_, result, raw, _ := f.request(t, url, "owner", "POST", "/mcp/servers/import", body)
		if strings.Contains(string(raw), "hidden-auth") {
			t.Fatal("credential exposed", string(raw))
		}
		if result["code"] == float64(common.CodeSuccess) {
			entries := result["data"].(map[string]any)["results"].([]any)
			if len(entries) != 1 || entries[0].(map[string]any)["success"] != false {
				t.Fatal("failed import became success", result)
			}
		}
		if before() != snapshot {
			t.Fatal("failed import mutated existing MCP")
		}
	}
	// Frozen test endpoint previews an unsaved URL; mcp_id is not stored ownership.
	for _, actor := range []string{"owner", "member"} {
		_, result, _, _ := f.request(t, url, actor, "POST", "/mcp/servers/preview/test", map[string]any{"url": "http://127.0.0.1:9/mcp", "server_type": "sse"})
		if result["code"] != float64(common.CodeDataError) {
			t.Fatal("original preview SSRF guard", result)
		}
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM mcp_server").Scan(&count); err != nil || count != 1 {
		t.Fatal("failed import SQL writes", count, err)
	}
	t.Run("original_list_filters_and_pages", func(t *testing.T) {
		for _, item := range []entity.MCPServer{
			{ID: f.Prefix + "second", TenantID: f.ids["owner"], Name: "second", URL: row.URL, ServerType: "sse", Headers: row.Headers, Variables: entity.JSONMap{}},
			{ID: f.Prefix + "third", TenantID: f.ids["owner"], Name: "third", URL: row.URL, ServerType: "sse", Headers: row.Headers, Variables: entity.JSONMap{}},
			{ID: f.Prefix + "foreign", TenantID: f.ids["foreign"], Name: "foreign", URL: row.URL, ServerType: "sse", Headers: row.Headers, Variables: entity.JSONMap{}},
		} {
			if err := f.DB.Create(&item).Error; err != nil {
				t.Fatal(err)
			}
		}
		for _, tc := range []struct {
			query string
			total int
			ids   []string
		}{
			{"?orderby=name&desc=false&page=1&page_size=1", 3, []string{row.ID}},
			{"?orderby=name&desc=false&page=2&page_size=1", 3, []string{f.Prefix + "second"}},
			{"?orderby=name&desc=false&page=4&page_size=1", 3, []string{}},
			{"?mcp_ids=" + row.ID, 1, []string{row.ID}},
			{"?mcp_ids=" + f.Prefix + "foreign", 0, []string{}},
			{"?keywords=SECOND", 1, []string{f.Prefix + "second"}},
			{"?keywords=no-matching-server", 0, []string{}},
			// The original DAO deliberately uses SQL LIKE; no local literal-wildcard dialect.
			{"?keywords=%25&orderby=name&desc=false", 3, []string{row.ID, f.Prefix + "second", f.Prefix + "third"}},
		} {
			_, result, raw, _ := f.request(t, url, "owner", "GET", "/mcp/servers"+tc.query, nil)
			if result["code"] != float64(common.CodeSuccess) {
				t.Fatal(tc.query, result)
			}
			data := result["data"].(map[string]any)
			items := data["mcp_servers"].([]any)
			if data["total"] != float64(tc.total) || len(items) != len(tc.ids) {
				t.Fatal("filtered total/selected page", tc.query, result)
			}
			for index, item := range items {
				entry := item.(map[string]any)
				if entry["id"] != tc.ids[index] {
					t.Fatal("original ordered page", tc.query, result)
				}
				if _, present := entry["headers"]; present || strings.Contains(string(raw), "hidden-auth") {
					t.Fatal("list exposed stored credentials", tc.query)
				}
			}
			if before() != snapshot {
				t.Fatal("list changed persisted credentials", tc.query)
			}
		}
		for _, query := range []string{"?page=bad", "?page_size=bad", "?page_size=1000000", "?orderby=unknown"} {
			_, result, _, _ := f.request(t, url, "owner", "GET", "/mcp/servers"+query, nil)
			if result["code"] == float64(common.CodeSuccess) || before() != snapshot {
				t.Fatal("malformed list changed SQL or succeeded", query, result)
			}
		}
		for _, actor := range []string{"disabled"} {
			_, result, _, _ := f.request(t, url, actor, "GET", "/mcp/servers", nil)
			if result["code"] == float64(common.CodeSuccess) {
				t.Fatal("inactive identity listed MCP servers", actor)
			}
		}
		// Revocation from another user's team does not disable an otherwise
		// active user's own MCP namespace. It must still reveal no owner rows.
		for _, actor := range []string{"invite", "revoked"} {
			_, result, _, _ := f.request(t, url, actor, "GET", "/mcp/servers", nil)
			if result["code"] != float64(common.CodeSuccess) || len(result["data"].(map[string]any)["mcp_servers"].([]any)) != 0 {
				t.Fatal("team relation broadened self-scoped MCP list", actor, result)
			}
		}
		if err := f.Read.QueryRow("SELECT count(*) FROM mcp_server").Scan(&count); err != nil || count != 4 || before() != snapshot {
			t.Fatal(fmt.Sprintf("list independent SQL readback count=%d", count), err)
		}
	})
	f.Journal.Event("canonical-mcp-http-readback", map[string]any{"actual_http_mysql": true, "tenant_crud_scope": true, "original_unsaved_preview": true, "ssrf_import_failure_zero_writes": true})
}
