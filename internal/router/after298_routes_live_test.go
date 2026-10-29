package router

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/admin"
	"multirag/internal/cli"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/testsupport"
	"multirag/internal/utility"
)

type after298MetadataEngine struct {
	engine.DocEngine
	calls atomic.Int64
	fail  atomic.Bool
}

func (e *after298MetadataEngine) Search(ctx context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	e.calls.Add(1)
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if len(req.IndexNames) != 1 || req.IndexNames[0] != "multirag_doc_meta_owner" || len(req.KbIDs) != 1 || req.KbIDs[0] != "dataset" {
		return nil, errors.New("unexpected metadata scope")
	}
	if e.fail.Load() {
		return nil, errors.New("private storage failure must be opaque")
	}
	return &types.SearchResult{Total: 1, Chunks: []map[string]interface{}{{"id": "document", "kb_id": "dataset", "meta_fields": map[string]interface{}{"author": "alice", "zero": 0}}}}, nil
}

// This case uses the actual routers, JWT/API middleware, SQL authorization and
// metadata service over TCP. Only the remote document index is controlled.
func TestAfter298RoutesHTTPAndSQL(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_AFTER298_I_DSN")
	if dsn == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned PostgreSQL required")
		}
		t.Skip("owned PostgreSQL required")
	}
	u, err := url.Parse(dsn)
	if err != nil || u.Hostname() != "127.0.0.1" || strings.TrimPrefix(u.Path, "/") != "multirag_after298_i_20261030" {
		t.Fatal("exact scratch database required")
	}
	j := testsupport.NewCase(t)
	reader, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("independent SQL open")
	}
	t.Cleanup(func() { reader.Close() })
	schema := "routes_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	var absent bool
	if err = reader.QueryRow("SELECT NOT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=$1)", schema).Scan(&absent); err != nil || !absent {
		t.Fatal("schema precondition")
	}
	j.Event("reserve-schema-before-create", map[string]any{"database": strings.TrimPrefix(u.Path, "/"), "schema": schema, "tables": []string{"t_ai_users", "t_ai_api_tokens", "t_ai_tenants", "t_ai_user_tenants", "t_ai_knowledgebases", "t_ai_documents"}})
	if _, err = reader.Exec(`CREATE SCHEMA "` + schema + `"`); err != nil {
		t.Fatal("schema create")
	}
	t.Cleanup(func() {
		if _, err := reader.Exec(`DROP SCHEMA "` + schema + `" CASCADE`); err != nil {
			t.Error("schema cleanup")
			return
		}
		var count int
		err := reader.QueryRow("SELECT count(*) FROM pg_namespace WHERE nspname=$1", schema).Scan(&count)
		j.Event("schema-cleanup-readback", map[string]any{"schema": schema, "absent": err == nil && count == 0})
		if err != nil || count != 0 {
			t.Error("schema survived")
		}
	})
	q := u.Query()
	q.Set("search_path", schema)
	u.RawQuery = q.Encode()
	db, err := gorm.Open(postgres.Open(u.String()), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("GORM connect")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	oldDB := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = oldDB })
	if err = db.AutoMigrate(&entity.User{}, &entity.APIToken{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.Knowledgebase{}, &entity.Document{}); err != nil {
		t.Fatal(err)
	}
	if err = common.Init("error"); err != nil {
		t.Fatal(err)
	}
	private := filepath.Join(os.Getenv("TMPDIR"), schema+".yaml")
	j.Event("reserve-private-signing-config", map[string]any{"path": private})
	if err = os.WriteFile(private, []byte("multirag:\n  secret_key: owned-after298-signing-fixture-32-characters\n"), 0600); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		err := os.Remove(private)
		_, statErr := os.Stat(private)
		j.Event("private-cleanup-readback", map[string]any{"path": private, "absent": os.IsNotExist(statErr)})
		if err != nil || !os.IsNotExist(statErr) {
			t.Error("private config survived")
		}
	})
	oldKey := server.GetSecretKey()
	if err = server.FromConfigFile(private); err != nil {
		t.Fatal("private config")
	}
	if err = server.InitVariables(nil); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { server.SetSecretKey(oldKey) })
	oldLicense := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned after298 test")
	t.Cleanup(func() { local.SetAdminStatus(oldLicense.Status, oldLicense.Reason) })
	must := func(value any) {
		t.Helper()
		if err := db.Create(value).Error; err != nil {
			t.Fatal(err)
		}
	}
	active, inactive, session := "1", "0", "owned-session"
	tokens := map[string]string{}
	j.Event("reserve-fixture-identities", map[string]any{"users": []string{"owner", "foreign", "disabled", "administrator"}, "tenant": "owner", "dataset": "dataset", "documents": []string{"document", "deleted"}, "schema": schema})
	for _, id := range []string{"owner", "foreign", "disabled", "administrator"} {
		status := active
		if id == "disabled" {
			status = inactive
		}
		super := id == "administrator"
		email := id + "@after298.invalid"
		must(&entity.User{ID: id, Email: email, Nickname: id, Status: &status, AccessToken: &session, IsActive: true, IsAuthenticated: true, IsSuperuser: &super})
		token, e := utility.SignUserJWT(email, server.GetSecretKey())
		if e != nil {
			t.Fatal(e)
		}
		tokens[id] = token
		if !super {
			must(&entity.APIToken{TenantID: id, Token: "api-" + id})
		}
	}
	must(&entity.Tenant{ID: "owner", Status: &active})
	must(&entity.UserTenant{ID: "membership", UserID: "owner", TenantID: "owner", Role: "owner", Status: &active})
	must(&entity.Knowledgebase{ID: "dataset", TenantID: "owner", CreatedBy: "owner", Name: "dataset", Permission: "me", Status: &active})
	must(&entity.Document{ID: "document", KbID: "dataset", CreatedBy: "owner", ParserConfig: entity.JSONMap{}, Name: &session, Status: &active})
	must(&entity.Document{ID: "deleted", KbID: "dataset", CreatedBy: "owner", ParserConfig: entity.JSONMap{}, Name: &session, Status: &inactive})
	index := &after298MetadataEngine{}
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	(&Router{authHandler: handler.NewAuthHandler(), documentHandler: handler.NewDocumentHandler(service.NewDocumentService(index))}).Setup(app)
	adminApp := gin.New()
	admin.NewRouter(admin.NewHandler(admin.NewService())).Setup(adminApp)
	start := func(address string, h http.Handler) *httptest.Server {
		t.Helper()
		host, port, e := net.SplitHostPort(address)
		if e != nil || host != "127.0.0.1" || port == "0" {
			t.Fatal("exact registered listener required")
		}
		j.Event("reserve-listener-before-bind", map[string]any{"address": address})
		l, e := net.Listen("tcp", address)
		if e != nil {
			t.Fatal(e)
		}
		s := &httptest.Server{Listener: l, Config: &http.Server{Handler: h}}
		s.Start()
		t.Cleanup(func() {
			s.Close()
			probe, e := net.Dial("tcp", address)
			if e == nil {
				probe.Close()
				t.Error("listener survived")
			}
			j.Event("listener-cleanup-readback", map[string]any{"address": address, "closed": e != nil})
		})
		return s
	}
	userHTTP := start(os.Getenv("MULTIRAG_AFTER298_I_USER_ADDRESS"), app)
	adminHTTP := start(os.Getenv("MULTIRAG_AFTER298_I_ADMIN_ADDRESS"), adminApp)
	request := func(base, method, path, auth string) (int, map[string]any, string) {
		t.Helper()
		req, e := http.NewRequest(method, base+path, nil)
		if e != nil {
			t.Fatal(e)
		}
		if auth != "" {
			req.Header.Set("Authorization", auth)
		}
		resp, e := http.DefaultClient.Do(req)
		if e != nil {
			t.Fatal(e)
		}
		defer resp.Body.Close()
		raw, e := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		if e != nil {
			t.Fatal(e)
		}
		var body map[string]any
		if json.Unmarshal(raw, &body) != nil {
			t.Fatal("JSON response required")
		}
		return resp.StatusCode, body, string(raw)
	}
	snapshot := func() string {
		t.Helper()
		var rows string
		if err := reader.QueryRow(`SELECT COALESCE(jsonb_agg(to_jsonb(x) ORDER BY id)::text,'[]') FROM "` + schema + `".t_ai_users x`).Scan(&rows); err != nil {
			t.Fatal("independent SQL readback")
		}
		return rows
	}
	before := snapshot()
	for _, auth := range []string{"", "Bearer invalid", "Bearer " + tokens["disabled"]} {
		for _, path := range []string{"/api/v1/agents/prompts", "/api/v1/datasets/dataset/metadata/summary"} {
			status, body, _ := request(userHTTP.URL, "GET", path, auth)
			if status != 401 || body["code"] == float64(0) {
				t.Fatal("authentication denial", status, body)
			}
		}
	}
	if index.calls.Load() != 0 || snapshot() != before {
		t.Fatal("denied auth had effects")
	}
	for _, auth := range []string{"Bearer " + tokens["owner"], "Bearer api-owner"} {
		status, body, _ := request(userHTTP.URL, "GET", "/api/v1/agents/prompts", auth)
		if status != 200 || body["code"] != float64(0) {
			t.Fatal("prompts", status, body)
		}
		data, ok := body["data"].(map[string]any)
		if !ok || len(data) != 4 {
			t.Fatal("complete prompts")
		}
		first, e1 := service.LoadPrompt("analyze_task_system")
		second, e2 := service.LoadPrompt("analyze_task_user")
		if e1 != nil || e2 != nil || data["task_analysis"] != first+"\n\n"+second {
			t.Fatal("prompt content mismatch")
		}
		for _, suffix := range []string{"", "?doc_ids=", "?doc_ids=document"} {
			status, body, raw := request(userHTTP.URL, "GET", "/api/v1/datasets/dataset/metadata/summary"+suffix, auth)
			if status != 200 || body["code"] != float64(0) || !strings.Contains(raw, "alice") {
				t.Fatal("metadata", status, body)
			}
		}
	}
	for _, c := range []struct {
		path, auth string
		status     int
	}{
		{"/api/v1/datasets/dataset/metadata/summary", "Bearer " + tokens["foreign"], 403},
		{"/api/v1/datasets/dataset/metadata/summary?doc_ids=deleted", "Bearer " + tokens["owner"], 404},
		{"/api/v1/datasets/dataset/metadata/summary?doc_ids=missing", "Bearer " + tokens["owner"], 404},
		{"/api/v1/datasets/dataset/metadata/summary?doc_ids=document,,missing", "Bearer " + tokens["owner"], 400},
		{"/api/v1/datasets/dataset/metadata/summary?doc_ids=document&doc_ids=document", "Bearer " + tokens["owner"], 400},
	} {
		calls := index.calls.Load()
		status, body, _ := request(userHTTP.URL, "GET", c.path, c.auth)
		if status != c.status || body["code"] == float64(0) || index.calls.Load() != calls {
			t.Fatal("scope denial", c.path, status, body)
		}
	}
	index.fail.Store(true)
	status, body, raw := request(userHTTP.URL, "GET", "/api/v1/datasets/dataset/metadata/summary", "Bearer "+tokens["owner"])
	if status != 500 || body["code"] == float64(0) || strings.Contains(raw, "private storage") {
		t.Fatal("opaque storage error", status, body)
	}
	index.fail.Store(false)
	if snapshot() != before {
		t.Fatal("read routes changed SQL")
	}
	status, _, _ = request(adminHTTP.URL, "GET", "/api/v1/admin/logout", tokens["administrator"])
	if status != 404 || snapshot() != before {
		t.Fatal("retired GET logout had effects")
	}
	status, _, _ = request(adminHTTP.URL, "POST", "/api/v1/admin/logout", tokens["owner"])
	if status != 403 || snapshot() != before {
		t.Fatal("non-admin logout")
	}
	adminURL, _ := url.Parse(adminHTTP.URL)
	host, port, _ := net.SplitHostPort(adminURL.Host)
	n, _ := strconv.Atoi(port)
	client := cli.NewMultiRAGClient("admin")
	client.HTTPClient.Host = host
	client.HTTPClient.Port = n
	client.HTTPClient.LoginToken = tokens["administrator"]
	if err = db.Callback().Update().Before("gorm:update").Register("after298-owned-failure", func(tx *gorm.DB) { tx.AddError(errors.New("owned update failure")) }); err != nil {
		t.Fatal(err)
	}
	if _, err = client.Logout(); err == nil || client.HTTPClient.LoginToken != tokens["administrator"] || snapshot() != before {
		t.Fatal("failed logout cleared session")
	}
	if err = db.Callback().Update().Remove("after298-owned-failure"); err != nil {
		t.Fatal(err)
	}
	if _, err = client.Logout(); err != nil || client.HTTPClient.LoginToken != "" {
		t.Fatal("POST logout failed", err)
	}
	var stored string
	if err = reader.QueryRow(`SELECT access_token FROM "` + schema + `".t_ai_users WHERE id='administrator'`).Scan(&stored); err != nil || !strings.HasPrefix(stored, "INVALID_") {
		t.Fatal("logout SQL readback")
	}
	j.Event("after298-acceptance", map[string]any{"actualRouters": true, "JWTAndAPIKey": true, "scopedSQLReadOnly": true, "controlledIndexCalls": index.calls.Load(), "adminPOSTLogout": true, "logoutFailureRetainsSession": true})
}
