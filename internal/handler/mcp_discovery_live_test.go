package handler_test

import (
	"database/sql"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/service"
)

// The transport is real TCP and SQL. Identity is supplied at the middleware
// boundary; JWT/API-token/beta authentication remains the existing auth suite.
func TestMCPManagementHTTPAndSQL(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_MCP_POSTGRES_DSN")
	if dsn == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned MCP scratch database required")
		}
		t.Skip("requires owned MCP scratch")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Discard})
	if err != nil {
		t.Fatal("scratch connection failed")
	}
	pool, _ := db.DB()
	defer pool.Close()
	read, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer read.Close()
	var dbname string
	if read.QueryRow("SELECT current_database()").Scan(&dbname) != nil || dbname != "multirag_after98_s_20261029" {
		t.Fatal("unowned database")
	}
	prior := dao.DB
	dao.DB = db
	defer func() { dao.DB = prior }()
	if db.AutoMigrate(&entity.Tenant{}, &entity.User{}, &entity.UserTenant{}, &entity.MCPServer{}) != nil {
		t.Fatal("migrate failed")
	}
	active := "1"
	for _, id := range []string{"h-owner", "h-member", "h-other"} {
		if db.Create(&entity.User{ID: id, Email: id + "@test.invalid", Nickname: id, Status: &active}).Error != nil {
			t.Fatal("fixture user")
		}
		if db.Create(&entity.Tenant{ID: id, Status: &active}).Error != nil {
			t.Fatal("fixture tenant")
		}
		role := "owner"
		if id == "h-member" {
			role = "normal"
		}
		if db.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: id, Role: role, Status: &active}).Error != nil {
			t.Fatal("fixture membership")
		}
	}
	row := entity.MCPServer{ID: "h-mcp", TenantID: "h-owner", Name: "owned", URL: "http://127.0.0.1:9/mcp?token=hidden-query", ServerType: "sse", Headers: entity.JSONMap{"Authorization": "hidden-auth"}, Variables: entity.JSONMap{}}
	if db.Create(&row).Error != nil {
		t.Fatal("fixture MCP")
	}
	snapshot := func() string {
		var raw string
		if read.QueryRow("SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY id)::text,'[]') FROM t_ai_mcp_server t").Scan(&raw) != nil {
			t.Fatal("SQL readback failed")
		}
		return raw
	}
	before := snapshot()
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.Use(func(c *gin.Context) {
		switch c.GetHeader("Authorization") {
		case "owner":
			c.Set("user", &entity.User{ID: "h-owner"})
		case "member":
			c.Set("user", &entity.User{ID: "h-member"})
		case "other":
			c.Set("user", &entity.User{ID: "h-other"})
		}
		c.Next()
	})
	h := handler.NewMCPHandler(service.NewMCPService())
	app.POST("/api/v1/mcp/servers/import", h.ImportMCPServers)
	app.POST("/api/v1/mcp/servers/:mcp_id/test", h.TestMCPServer)
	app.GET("/api/v1/mcp/servers/:mcp_id/tools", h.GetMCPServerTools)
	api := mcpOwnedHTTPServer(t, app)
	client := &http.Client{Timeout: 3 * time.Second}
	check := func(method, path, identity, body string, want common.ErrorCode) map[string]any {
		t.Helper()
		request, err := http.NewRequestWithContext(t.Context(), method, api.URL+path, strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		request.Header.Set("Content-Type", "application/json")
		request.Header.Set("Authorization", identity)
		response, err := client.Do(request)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		raw, err := io.ReadAll(response.Body)
		if err != nil {
			t.Fatal(err)
		}
		var value map[string]any
		if response.StatusCode != 200 || json.Unmarshal(raw, &value) != nil || value["code"] != float64(want) {
			t.Fatal("business response", response.StatusCode, string(raw))
		}
		if strings.Contains(string(raw), "hidden-") {
			t.Fatal("credential leaked")
		}
		return value
	}
	body := `{"url":"http://127.0.0.1:9/mcp?token=hidden-query","server_type":"sse","headers":{"Authorization":"hidden-auth"}}`
	check("POST", "/api/v1/mcp/servers/preview/test", "member", body, common.CodeForbidden)
	check("POST", "/api/v1/mcp/servers/preview/test", "owner", body, common.CodeDataError)
	check("GET", "/api/v1/mcp/servers/h-mcp/tools", "other", "", common.CodeForbidden)
	check("GET", "/api/v1/mcp/servers/h-mcp/tools", "owner", "", common.CodeDataError)
	check("GET", "/api/v1/mcp/servers/h-mcp/tools?timeout=10000", "owner", "", common.CodeArgumentError)
	check("GET", "/api/v1/mcp/servers/h-mcp/tools?timeout=%ZZ", "owner", "", common.CodeArgumentError)
	check("GET", "/api/v1/mcp/servers/h-mcp/tools?timeout=1&timeout=2", "owner", "", common.CodeArgumentError)
	check("POST", "/api/v1/mcp/servers/preview/test", "owner", `{"url":"https://example.com","server_type":"sse","headers":{"Authorization":null}}`, common.CodeArgumentError)
	nullResult := check("POST", "/api/v1/mcp/servers/import", "owner", `{"mcpServers":{"null-header":{"type":"sse","url":"https://example.com","headers":{"Authorization":null}}}}`, common.CodeSuccess)
	if nullResult["data"].(map[string]any)["results"].([]any)[0].(map[string]any)["success"] != false {
		t.Fatal("null header imported")
	}
	result := check("POST", "/api/v1/mcp/servers/import", "owner", `{"mcpServers":{"blocked":{"type":"sse","url":"http://127.0.0.1:9/mcp"}}}`, common.CodeSuccess)
	entry := result["data"].(map[string]any)["results"].([]any)[0].(map[string]any)
	if entry["success"] != false {
		t.Fatal("SSRF rejection falsely imported")
	}
	if after := snapshot(); after != before {
		t.Fatal("denied/failed/read-only HTTP changed SQL")
	}
	t.Log("real HTTP business codes and independent SQL readback: owner/member/foreign, SSRF failure, strict timeout, import failure zero writes, credential redaction")
}
