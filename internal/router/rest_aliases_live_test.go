package router

import (
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/server/local"
	"multirag/internal/service"
)

func TestRESTAliasesRequireAuthentication(t *testing.T) {
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	(&Router{authHandler: handler.NewAuthHandler()}).Setup(app)
	for _, entry := range [][2]string{{"GET", "/api/v1/users/me/models"}, {"PATCH", "/api/v1/users/me/models"}, {"GET", "/api/v1/tenant/list"}, {"GET", "/api/v1/files/id/parent"}, {"GET", "/api/v1/connectors/"}} {
		w := httptest.NewRecorder()
		app.ServeHTTP(w, httptest.NewRequest(entry[0], entry[1], strings.NewReader(`{}`)))
		if w.Code != http.StatusUnauthorized {
			t.Fatalf("%v: status=%d body=%s", entry, w.Code, w.Body)
		}
	}
}

func TestRESTAliasesHTTPAndSQL(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_AFTER9D_B_ALIASES_DSN")
	if dsn == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned alias scratch database required")
		}
		t.Skip("requires owned alias scratch database")
	}
	parsed, err := url.Parse(dsn)
	if err != nil || !strings.HasPrefix(strings.TrimPrefix(parsed.Path, "/"), "multirag_after9d_b_aliases_") {
		t.Fatal("unowned scratch database")
	}
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal(err)
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { readback.Close() })
	prev := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = prev })
	previousAdmin := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned alias test")
	t.Cleanup(func() { local.SetAdminStatus(previousAdmin.Status, previousAdmin.Reason) })
	if err := db.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.APIToken{}, &entity.Connector{}, &entity.File{}, &entity.File2Document{}, &entity.Document{}); err != nil {
		t.Fatal(err)
	}
	active, access := "1", "owned-access-session"
	for _, id := range []string{"alias-owner", "alias-foreign"} {
		for _, row := range []any{
			&entity.User{ID: id, Email: id + "@example.invalid", Nickname: id, AccessToken: &access, Status: &active},
			&entity.Tenant{ID: id, EmbdID: "embed", LLMID: "before", Status: &active},
			&entity.UserTenant{ID: id, UserID: id, TenantID: id, Role: "owner", InvitedBy: id, Status: &active},
			&entity.APIToken{TenantID: id, Token: "alias-token-" + id},
			&entity.Connector{ID: id, TenantID: id, Name: id, Source: "s3", InputType: "poll", Config: entity.JSONMap{"secret": "never-expose"}, Status: "schedule"},
			&entity.File{ID: id, ParentID: id, TenantID: id, CreatedBy: id, Name: "/", Type: "folder"},
			&entity.File{ID: id + "-child", ParentID: id, TenantID: id, CreatedBy: id, Name: "child", Type: "txt"},
		} {
			if err := db.Create(row).Error; err != nil {
				t.Fatal(err)
			}
		}
	}
	users := service.NewUserService()
	app := gin.New()
	(&Router{authHandler: handler.NewAuthHandler(), userHandler: handler.NewUserHandler(users), tenantHandler: handler.NewTenantHandler(service.NewTenantService(), users), fileHandler: handler.NewFileHandler(service.NewFileService(), users), connectorHandler: handler.NewConnectorHandler(service.NewConnectorService(), users)}).Setup(app)
	request := func(method, path, body, actor string) (int, map[string]any) {
		t.Helper()
		w := httptest.NewRecorder()
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		if actor != "" {
			req.Header.Set("Authorization", "Bearer alias-token-"+actor)
		}
		app.ServeHTTP(w, req)
		var out map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
			t.Fatalf("%s: %s", path, w.Body)
		}
		return w.Code, out
	}
	snapshot := func() string {
		t.Helper()
		var raw string
		if err := readback.QueryRow(`SELECT (SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM t_ai_tenants x) || (SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM t_ai_files x)`).Scan(&raw); err != nil {
			t.Fatal(err)
		}
		return raw
	}
	before := snapshot()
	for _, path := range []string{"/api/v1/users/me/models", "/api/v1/tenant/list", "/api/v1/connectors/", "/api/v1/files/alias-owner-child/parent", "/api/v1/files/alias-owner-child/parent?file_id=alias-owner-child", "/v1/file/parent_folder?file_id=alias-owner-child"} {
		status, out := request("GET", path, "", "alias-owner")
		if status != 200 || out["code"] != float64(0) {
			t.Fatalf("%s: %d %v", path, status, out)
		}
		encoded, _ := json.Marshal(out["data"])
		if strings.Contains(string(encoded), "alias-foreign") || strings.Contains(string(encoded), "never-expose") {
			t.Fatalf("tenant data leaked: %s", encoded)
		}
	}
	if snapshot() != before {
		t.Fatal("GET changed SQL state")
	}
	for _, tc := range []struct{ method, path, body, actor string }{
		{"GET", "/api/v1/files/alias-owner-child/parent", "", "alias-foreign"},
		{"GET", "/api/v1/files/alias-owner-child/parent?file_id=alias-foreign-child", "", "alias-owner"},
		{"PATCH", "/api/v1/users/me/models", `{"tenant_id":"alias-foreign","llm_id":"forbidden"}`, "alias-owner"},
		{"PATCH", "/api/v1/users/me/models", `{"tenant_id":"alias-owner","llm_id":"forbidden"}`, ""},
	} {
		_, out := request(tc.method, tc.path, tc.body, tc.actor)
		if out["code"] == float64(0) {
			t.Fatalf("unauthorized/conflicting request succeeded: %v", tc)
		}
		if snapshot() != before {
			t.Fatalf("denied request changed SQL: %v", tc)
		}
	}
	_, out := request("PATCH", "/api/v1/users/me/models", `{"llm_id":"after"}`, "alias-owner")
	if out["code"] != float64(0) {
		t.Fatal(out)
	}
	var llm string
	if err := readback.QueryRow(`SELECT llm_id FROM t_ai_tenants WHERE id='alias-owner'`).Scan(&llm); err != nil || llm != "after" {
		t.Fatal("missing committed model update", err)
	}
	if err := readback.QueryRow(`SELECT llm_id FROM t_ai_tenants WHERE id='alias-foreign'`).Scan(&llm); err != nil || llm != "before" {
		t.Fatal("foreign model changed", err)
	}
}
