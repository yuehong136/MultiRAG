package router

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v4"
	"github.com/google/uuid"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/testsupport"
	"multirag/internal/utility"
)

// after98RouterServer reserves an exact loopback address before binding it.
func after98RouterServer(t *testing.T, journal *testsupport.CaseJournal, role string, handler http.Handler) string {
	t.Helper()
	id := uuid.New()
	address := fmt.Sprintf("127.0.0.1:%d", 20000+(int(id[0])<<8|int(id[1]))%30000)
	journal.Event("reserve-listener", map[string]any{"network": "tcp4", "address": address, "role": role})
	listener, err := net.Listen("tcp4", address)
	if err != nil {
		t.Fatal("bind reserved listener", err)
	}
	api := &http.Server{Handler: handler, ReadHeaderTimeout: time.Second}
	done := make(chan error, 1)
	go func() { done <- api.Serve(listener) }()
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		shutdownErr := api.Shutdown(ctx)
		if shutdownErr != nil {
			_ = api.Close()
		}
		serveErr := <-done
		probe, probeErr := net.DialTimeout("tcp4", address, time.Second)
		if probe != nil {
			_ = probe.Close()
		}
		absent := probeErr != nil
		journal.Event("cleanup-listener", map[string]any{"address": address, "role": role, "absent": absent, "shutdown_ok": shutdownErr == nil, "serve_closed": errors.Is(serveErr, http.ErrServerClosed)})
		if shutdownErr != nil || !errors.Is(serveErr, http.ErrServerClosed) || !absent {
			t.Error("owned HTTP listener did not drain", shutdownErr, serveErr)
		}
	})
	return "http://" + address
}

// TestAfter98RouterContractsJWTAPIAndSQL uses production Router.Setup and auth;
// remote MCP success is covered separately, while this test proves fail-closed
// management/SSRF behavior without adding a production loopback bypass.
func TestAfter98RouterContractsJWTAPIAndSQL(t *testing.T) {
	configPath := os.Getenv("MULTIRAG_AFTER98_ROUTER_CONFIG")
	if configPath == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("private after98 router PostgreSQL config required")
		}
		t.Skip("requires owned PostgreSQL scratch resources")
	}
	journal := testsupport.NewCase(t)
	var cfg struct {
		AdminDSN   string `json:"admin_dsn"`
		PGEndpoint string `json:"pg_endpoint"`
	}
	raw, err := os.ReadFile(configPath)
	if err != nil || json.Unmarshal(raw, &cfg) != nil {
		t.Fatal("private router config unavailable")
	}
	parsed, err := url.Parse(cfg.AdminDSN)
	if err != nil || parsed.Host != "127.0.0.1:5432" || cfg.PGEndpoint != parsed.Host || (parsed.Scheme != "postgres" && parsed.Scheme != "postgresql") {
		t.Fatal("explicit PostgreSQL TCP endpoint required")
	}
	admin, err := sql.Open("pgx", cfg.AdminDSN)
	if err != nil {
		t.Fatal("open admin connection")
	}
	t.Cleanup(func() { _ = admin.Close() })
	database := "multirag_after98_router_" + strings.ReplaceAll(uuid.NewString(), "-", "")[:16]
	journal.Event("reserve-database", map[string]any{"endpoint": cfg.PGEndpoint, "database": database, "config_file": configPath})
	var present bool
	if err = admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", database).Scan(&present); err != nil || present {
		t.Fatal("scratch database absence precondition", err)
	}
	t.Cleanup(func() {
		_, dropErr := admin.Exec(`DROP DATABASE IF EXISTS "` + database + `" WITH (FORCE)`)
		readErr := admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", database).Scan(&present)
		journal.Event("cleanup-database", map[string]any{"endpoint": cfg.PGEndpoint, "database": database, "absent": readErr == nil && !present, "drop_ok": dropErr == nil})
		if dropErr != nil || readErr != nil || present {
			t.Error("owned database cleanup", dropErr, readErr)
		}
	})
	if _, err = admin.Exec(`CREATE DATABASE "` + database + `"`); err != nil {
		t.Fatal("create owned database", err)
	}
	parsed.Path = "/" + database
	query := parsed.Query()
	query.Set("search_path", "usr_ai,public")
	parsed.RawQuery = query.Encode()
	db, err := gorm.Open(postgres.Open(parsed.String()), &gorm.Config{Logger: logger.Discard})
	if err != nil {
		t.Fatal("connect owned database")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = pool.Close() })
	reader, err := sql.Open("pgx", parsed.String())
	if err != nil {
		t.Fatal("open independent reader")
	}
	t.Cleanup(func() { _ = reader.Close() })
	previousDB := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = previousDB })
	tables := []string{"t_ai_users", "t_ai_tenants", "t_ai_user_tenants", "t_ai_api_tokens", "t_ai_user_canvases", "t_ai_user_canvas_version", "t_ai_canvas_templates", "t_ai_mcp_server"}
	journal.Event("reserve-schema", map[string]any{"endpoint": cfg.PGEndpoint, "database": database, "schema": "usr_ai", "tables": tables})
	if err = db.Exec("CREATE SCHEMA usr_ai").Error; err != nil {
		t.Fatal("create owned schema", err)
	}
	if err = db.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.APIToken{}, &entity.UserCanvas{}, &entity.UserCanvasVersion{}, &entity.CanvasTemplate{}, &entity.MCPServer{}); err != nil {
		t.Fatal("migrate owned tables", err)
	}
	var schema string
	if err = reader.QueryRow("SELECT current_schema()").Scan(&schema); err != nil || schema != "usr_ai" {
		t.Fatal("scratch reader schema mismatch", err)
	}

	tempRoot := os.Getenv("TMPDIR")
	if !filepath.IsAbs(tempRoot) {
		t.Fatal("registered absolute TMPDIR required")
	}
	secret := uuid.NewString() + uuid.NewString()
	keyPath := filepath.Join(tempRoot, "after98-router-"+uuid.NewString()+".yaml")
	keyBytes := []byte("server:\n  secret_key: " + secret + "\n")
	keyHash := sha256.Sum256(keyBytes)
	journal.Event("reserve-secret-config", map[string]any{"path": keyPath, "sha256": hex.EncodeToString(keyHash[:])})
	if err = os.WriteFile(keyPath, keyBytes, 0600); err != nil {
		t.Fatal("write owned signing config")
	}
	t.Cleanup(func() {
		removeErr := os.Remove(keyPath)
		_, statErr := os.Stat(keyPath)
		journal.Event("cleanup-secret-config", map[string]any{"path": keyPath, "absent": os.IsNotExist(statErr)})
		if removeErr != nil || !os.IsNotExist(statErr) {
			t.Error("owned signing config cleanup", removeErr, statErr)
		}
	})
	previousKey := server.GetSecretKey()
	var previousConfig *server.Config
	if current := server.GetConfig(); current != nil {
		copy := *current
		previousConfig = &copy
	}
	t.Cleanup(func() {
		server.SetSecretKey(previousKey)
		if previousConfig != nil && server.GetConfig() != nil {
			*server.GetConfig() = *previousConfig
		}
	})
	server.SetSecretKey("")
	if err = server.FromConfigFile(keyPath); err != nil {
		t.Fatal("load owned signing config")
	}
	if err = server.InitVariables(nil); err != nil || server.GetSecretKey() != secret {
		t.Fatal("initialize owned signing key", err)
	}
	previousAdmin := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned after98 router test")
	t.Cleanup(func() { local.SetAdminStatus(previousAdmin.Status, previousAdmin.Reason) })

	actors := []string{"owner", "member", "foreign", "disabled", "super"}
	journal.Event("reserve-sql-identities", map[string]any{"database": database, "schema": schema, "users": actors, "tenants": actors, "memberships": append(append([]string{}, actors...), "member-team"), "canvases": []string{"owned", "team"}, "versions": []string{"owned-version"}, "templates": []string{"template-b", "template-a", "template-old"}, "mcp": []string{"owned-mcp"}})
	active := "1"
	tokens := map[string]string{}
	for _, id := range actors {
		session := "session-" + uuid.NewString()
		super := id == "super"
		user := entity.User{ID: id, Email: id + "@after98.invalid", Nickname: id, AccessToken: &session, Status: &active, IsActive: true, IsAuthenticated: true, IsSuperuser: &super}
		if err = db.Create(&user).Error; err != nil {
			t.Fatal("seed owned user", err)
		}
		if err = db.Create(&entity.Tenant{ID: id, Status: &active}).Error; err != nil {
			t.Fatal("seed owned tenant", err)
		}
		role := "owner"
		if id == "member" {
			role = "normal"
		}
		if err = db.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: id, Role: role, Status: &active}).Error; err != nil {
			t.Fatal("seed owner membership", err)
		}
		tokens[id], err = utility.SignUserJWT(user.Email, secret)
		if err != nil {
			t.Fatal("sign JWT")
		}
		apiKey := "multirag-" + strings.ReplaceAll(uuid.NewString(), "-", "")
		hash := sha256.Sum256([]byte(apiKey))
		journal.Event("reserve-api-token", map[string]any{"tenant_id": id, "token_sha256": hex.EncodeToString(hash[:])})
		if err = db.Create(&entity.APIToken{TenantID: id, Token: apiKey}).Error; err != nil {
			t.Fatal("seed owned API token", err)
		}
		tokens["api-"+id] = apiKey
	}
	if err = db.Create(&entity.UserTenant{ID: "member-team", UserID: "member", TenantID: "owner", Role: "normal", Status: &active}).Error; err != nil {
		t.Fatal("seed team membership", err)
	}
	if err = db.Model(&entity.User{}).Where("id = ?", "disabled").Update("is_active", false).Error; err != nil {
		t.Fatal("disable fixture account", err)
	}
	tokens["invalid"] = "invalid"
	tokens["wrong"], err = utility.SignUserJWT("owner@after98.invalid", "another-owned-signing-key")
	if err != nil {
		t.Fatal("sign negative JWT", err)
	}
	tokens["expired"], err = jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.RegisteredClaims{Subject: "owner@after98.invalid", ExpiresAt: jwt.NewNumericDate(time.Now().Add(-time.Hour))}).SignedString([]byte(secret))
	if err != nil {
		t.Fatal("sign expired JWT", err)
	}
	for _, id := range []string{"owned", "team"} {
		stamp := int64(1)
		if err = db.Create(&entity.UserCanvas{ID: id, UserID: "owner", Permission: "team", Tags: "protected", DSL: entity.JSONMap{"nodes": []any{"preserve"}}, BaseModel: entity.BaseModel{UpdateTime: &stamp}}).Error; err != nil {
			t.Fatal("seed canvas", err)
		}
	}
	if err = db.Create(&entity.UserCanvasVersion{ID: "owned-version", UserCanvasID: "owned", DSL: entity.JSONMap{"saved": true}}).Error; err != nil {
		t.Fatal("seed canvas version", err)
	}
	for _, id := range []string{"template-b", "template-a", "template-old"} {
		stamp := int64(100)
		if id == "template-old" {
			stamp = 1
		}
		if err = db.Create(&entity.CanvasTemplate{ID: id, Title: entity.JSONMap{"zh": "标题", "en": "Title"}, Description: entity.JSONMap{"zh": "说明", "en": "Description"}, DSL: entity.JSONMap{"nodes": []any{"starter"}}, BaseModel: entity.BaseModel{CreateTime: &stamp}}).Error; err != nil {
			t.Fatal("seed multilingual template", err)
		}
	}
	var outbound atomic.Int64
	trap := after98RouterServer(t, journal, "forbidden-MCP-destination", http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { outbound.Add(1); w.WriteHeader(http.StatusNoContent) }))
	if err = db.Create(&entity.MCPServer{ID: "owned-mcp", TenantID: "owner", Name: "owned", URL: trap + "/mcp?token=hidden-query", ServerType: "sse", Headers: entity.JSONMap{"Authorization": "hidden-auth"}, Variables: entity.JSONMap{"tools": map[string]any{}}}).Error; err != nil {
		t.Fatal("seed owned MCP server", err)
	}
	snapshot := func(table string) string {
		t.Helper()
		var value string
		if err := reader.QueryRow("SELECT COALESCE(jsonb_agg(to_jsonb(r) ORDER BY to_jsonb(r)::text),'[]'::jsonb)::text FROM usr_ai." + table + " r").Scan(&value); err != nil {
			t.Fatal("independent SQL snapshot", table, err)
		}
		return value
	}
	before := map[string]string{}
	for _, table := range tables {
		before[table] = snapshot(table)
	}
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	NewRouter(handler.NewAuthHandler(), nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil).Setup(app)
	base := after98RouterServer(t, journal, "actual-Router.Setup", app)
	client := &http.Client{Transport: &http.Transport{Proxy: nil}, Timeout: 5 * time.Second}
	t.Cleanup(client.CloseIdleConnections)
	request := func(method, path, body, actor string, status int, code common.ErrorCode) map[string]any {
		t.Helper()
		req, err := http.NewRequestWithContext(t.Context(), method, base+path, strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		if actor != "" {
			req.Header.Set("Authorization", "Bearer "+tokens[actor])
		}
		response, err := client.Do(req)
		if err != nil {
			t.Fatal("actual router HTTP request", err)
		}
		defer response.Body.Close()
		raw, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
		var value map[string]any
		if err != nil || response.StatusCode != status || json.Unmarshal(raw, &value) != nil || value["code"] != float64(code) {
			t.Fatalf("%s %s actor=%s: status=%d body=%s, want status=%d code=%d", method, path, actor, response.StatusCode, raw, status, code)
		}
		if strings.Contains(string(raw), "hidden-") {
			t.Fatal("MCP credential reflected in response")
		}
		return value
	}
	preview := fmt.Sprintf(`{"url":%q,"server_type":"sse","headers":{"Authorization":"hidden-auth"}}`, trap+"/mcp?token=hidden-query")
	importBody := fmt.Sprintf(`{"mcpServers":{"blocked":{"type":"sse","url":%q}}}`, trap+"/mcp")
	paths := []struct{ method, path, body string }{{"GET", "/api/v1/agents/templates", ""}, {"PUT", "/api/v1/agents/owned/tags", `{"tags":[]}`}, {"POST", "/api/v1/mcp/servers/preview/test", preview}, {"GET", "/api/v1/mcp/servers/owned-mcp/tools", ""}, {"POST", "/api/v1/mcp/servers/import", importBody}}
	for _, actor := range []string{"", "invalid", "wrong", "expired", "disabled", "api-disabled", "super", "api-super"} {
		status, code := http.StatusUnauthorized, common.CodeUnauthorized
		if actor == "super" || actor == "api-super" {
			status, code = http.StatusForbidden, common.CodeForbidden
		}
		for _, route := range paths {
			request(route.method, route.path, route.body, actor, status, code)
		}
	}
	for _, table := range tables {
		if snapshot(table) != before[table] {
			t.Fatal("middleware denial changed SQL", table)
		}
	}
	for _, actor := range []string{"owner", "api-owner"} {
		rows := request("GET", "/api/v1/agents/templates", "", actor, 200, common.CodeSuccess)["data"].([]any)
		if len(rows) != 3 {
			t.Fatal("template catalogue shape", rows)
		}
		for i, id := range []string{"template-a", "template-b", "template-old"} {
			row := rows[i].(map[string]any)
			if row["id"] != id || row["title"].(map[string]any)["zh"] != "标题" || row["description"].(map[string]any)["en"] != "Description" || row["dsl"].(map[string]any)["nodes"].([]any)[0] != "starter" {
				t.Fatal("literal template route lost locale/DSL/order", row)
			}
		}
	}
	for _, actor := range []string{"owner", "api-owner"} {
		for _, body := range []string{`{}`, `{"tags":null}`, `{"tags":[null]}`, `{"tags":[1]}`, `{"tags":[],"owner":"foreign"}`, `{"tags":[],"tags":["x"]}`} {
			request("PUT", "/api/v1/agents/owned/tags", body, actor, 200, common.CodeArgumentError)
		}
	}
	for _, actor := range []string{"member", "api-member", "foreign", "api-foreign"} {
		request("PUT", "/api/v1/agents/owned/tags", `{"tags":["denied"]}`, actor, 200, common.CodeOperatingError)
	}
	if snapshot("t_ai_user_canvases") != before["t_ai_user_canvases"] {
		t.Fatal("invalid/member/foreign tag request changed SQL")
	}
	canvasFields := func() string {
		t.Helper()
		var value string
		if err := reader.QueryRow("SELECT (to_jsonb(r)-'tags'-'update_time'-'update_date')::text FROM usr_ai.t_ai_user_canvases r WHERE id='owned'").Scan(&value); err != nil {
			t.Fatal("independent canvas field readback", err)
		}
		return value
	}
	originalCanvasFields := canvasFields()
	request("PUT", "/api/v1/agents/owned/tags", `{"tags":[" ML ","ml","Data,Science","中文"]}`, "owner", 200, common.CodeSuccess)
	var tags string
	var updated int64
	if err = reader.QueryRow("SELECT tags,update_time FROM usr_ai.t_ai_user_canvases WHERE id='owned'").Scan(&tags, &updated); err != nil || tags != "ML,Data Science,中文" || updated <= 1 {
		t.Fatal("tag normalization/timestamp not persisted", tags, updated, err)
	}
	request("PUT", "/api/v1/agents/owned/tags", `{"tags":[]}`, "api-owner", 200, common.CodeSuccess)
	if err = reader.QueryRow("SELECT tags FROM usr_ai.t_ai_user_canvases WHERE id='owned'").Scan(&tags); err != nil || tags != "" {
		t.Fatal("API-key explicit empty tags did not clear", err)
	}
	if canvasFields() != originalCanvasFields {
		t.Fatal("tag mutation changed unrelated owned canvas fields")
	}
	var unaffected bool
	if err = reader.QueryRow(`SELECT tags='protected' AND dsl='{"nodes":["preserve"]}'::jsonb FROM usr_ai.t_ai_user_canvases WHERE id='team'`).Scan(&unaffected); err != nil || !unaffected {
		t.Fatal("unrelated canvas changed", err)
	}
	for _, actor := range []string{"member", "api-member"} {
		request("POST", "/api/v1/mcp/servers/preview/test", preview, actor, 200, common.CodeForbidden)
		request("POST", "/api/v1/mcp/servers/import", importBody, actor, 200, common.CodeForbidden)
	}
	for _, actor := range []string{"foreign", "api-foreign"} {
		request("GET", "/api/v1/mcp/servers/owned-mcp/tools", "", actor, 200, common.CodeForbidden)
	}
	for _, actor := range []string{"owner", "api-owner"} {
		request("POST", "/api/v1/mcp/servers/preview/test", preview, actor, 200, common.CodeDataError)
		request("GET", "/api/v1/mcp/servers/owned-mcp/tools", "", actor, 200, common.CodeDataError)
		result := request("POST", "/api/v1/mcp/servers/import", importBody, actor, 200, common.CodeSuccess)
		entries := result["data"].(map[string]any)["results"].([]any)
		if len(entries) != 1 || entries[0].(map[string]any)["success"] != false {
			t.Fatal("SSRF failure falsely imported", result)
		}
	}
	canvasAfterUpdates := snapshot("t_ai_user_canvases")
	local.SetAdminStatus(1, "owned unavailable test")
	request("PUT", "/api/v1/agents/owned/tags", `{"tags":["license-denied"]}`, "owner", 503, common.CodeUnauthorized)
	local.SetAdminStatus(0, "owned after98 router test")
	if snapshot("t_ai_user_canvases") != canvasAfterUpdates {
		t.Fatal("license denial changed canvas SQL")
	}
	for _, table := range tables {
		if table != "t_ai_user_canvases" && snapshot(table) != before[table] {
			t.Fatal("read-only/failed management request changed SQL", table)
		}
	}
	if outbound.Load() != 0 {
		t.Fatal("forbidden MCP endpoint received a request", outbound.Load())
	}
	journal.Event("router-contract-readback", map[string]any{"database": database, "schema": schema, "auth": "actual JWT/API token middleware", "literal_templates": true, "tags_owner_only": true, "empty_tags_clear": true, "mcp_denials_zero_new_rows": true, "outbound_requests": outbound.Load(), "independent_sql_reader": true})
}
