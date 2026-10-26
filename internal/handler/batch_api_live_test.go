package handler_test

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"multirag/internal/admin"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/utility"
)

type batchGReadEngine struct {
	engine.DocEngine
	calls atomic.Int64
}

func (e *batchGReadEngine) Search(_ context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	e.calls.Add(1)
	if len(req.KbIDs) == 0 || req.Filter["doc_id"] != "g-shared-doc" {
		return nil, fmt.Errorf("invalid read scope")
	}
	return &types.SearchResult{Total: 1, Chunks: []map[string]any{{"id": "g-chunk", "content": "owned content"}}}, nil
}

// TestBatchGoAPI exercises real JWT routes and PostgreSQL, with separate SQL readback.
// The harness must reserve the entire scratch DB and exact listener before startup.
func TestBatchGoAPI(t *testing.T) {
	config := os.Getenv("MULTIRAG_BATCH_G_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned batch G PostgreSQL config required")
		}
		t.Skip("requires owned batch G PostgreSQL config")
	}
	if readOwnedOptInResources(t) == nil {
		t.Fatal("durable exact resource manifest required")
	}
	root := filepath.Join("..", "..")
	if _, err := os.Stat("go.mod"); err == nil {
		root = "."
	}
	t.Chdir(root)
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal("private config unavailable")
	}
	cfg := server.GetConfig()
	if cfg.Database.Host != "127.0.0.1" || !strings.HasPrefix(cfg.Database.Database, "multirag_batch_g_") {
		t.Fatal("unowned database")
	}
	recordOwnedOptInResource(t, "before-create", "sql-schema", cfg.Database.Database+":public")
	if err := dao.InitDB(); err != nil {
		t.Fatal(err)
	}
	pool, _ := dao.DB.DB()
	t.Cleanup(func() { pool.Close() })
	dsn, err := dao.PostgresDSN(cfg.Database)
	if err != nil {
		t.Fatal(err)
	}
	read, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { read.Close() })
	if err := server.InitVariables(nil); err != nil {
		t.Fatal(err)
	}
	local.InitAdminStatus(0, "owned batch API acceptance")
	recordOwnedOptInTestBinary(t)
	execSQL := func(q string, args ...any) {
		t.Helper()
		if _, err := read.Exec(q, args...); err != nil {
			t.Fatal("owned SQL statement failed", err)
		}
	}
	count := func(table string) int {
		t.Helper()
		var n int
		if err := read.QueryRow("SELECT count(*) FROM " + table).Scan(&n); err != nil {
			t.Fatal(err)
		}
		return n
	}
	snapshot := func(table string) string {
		t.Helper()
		var value string
		if err := read.QueryRow("SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text,'[]') FROM " + table + " t").Scan(&value); err != nil {
			t.Fatal(err)
		}
		return value
	}
	active := "1"
	tokens := map[string]string{}
	for _, id := range []string{"g-owner", "g-member", "g-other", "g-invite", "g-inactive"} {
		row := entity.User{ID: id, Email: id + "@owned.invalid", Nickname: id, Status: &active, IsActive: true, IsAuthenticated: true}
		if err := dao.DB.Create(&row).Error; err != nil {
			t.Fatal(err)
		}
		name := id
		if err := dao.DB.Create(&entity.Tenant{ID: id, Name: &name, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := dao.DB.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: id, Role: "owner", InvitedBy: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		token, err := utility.SignUserJWT(row.Email, server.GetSecretKey())
		if err != nil {
			t.Fatal(err)
		}
		tokens[id] = token
	}
	for _, row := range []entity.UserTenant{{ID: "g-team", UserID: "g-member", TenantID: "g-owner", Role: "normal", Status: &active}, {ID: "g-pending", UserID: "g-invite", TenantID: "g-owner", Role: "invite", Status: &active}} {
		if err := dao.DB.Create(&row).Error; err != nil {
			t.Fatal(err)
		}
	}
	execSQL("INSERT INTO t_ai_user_tenants(id,user_id,tenant_id,role,status,invited_by) VALUES ('g-disabled','g-inactive','g-owner','normal','0','g-owner')")
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.Use(gin.Recovery())
	us := service.NewUserService()
	ks := service.NewKnowledgebaseService()
	ts := service.NewTenantService()
	cs := service.NewConnectorService()
	readEngine := &batchGReadEngine{}
	chunkService := service.NewChunkService(readEngine)
	routes := router.NewRouter(handler.NewAuthHandler(), handler.NewUserHandler(us), handler.NewTenantHandler(ts, us), nil, nil, handler.NewSystemHandler(service.NewSystemService()), handler.NewKnowledgebaseHandler(ks, us, nil), handler.NewChunkHandler(chunkService, us), nil, handler.NewChatHandler(service.NewChatService(), us), nil, handler.NewConnectorHandler(cs, us), handler.NewSearchHandler(service.NewSearchService(), us), nil, nil, nil, nil)
	routes.Setup(app)
	api := newOwnedOptInHTTPServer(t, "batch-api", app)
	client := &http.Client{Timeout: 20 * time.Second}
	request := func(method, path, identity, body string) (int, map[string]any, string) {
		t.Helper()
		req, err := http.NewRequestWithContext(t.Context(), method, api.URL+path, strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		if identity != "" {
			req.Header.Set("Authorization", "Bearer "+tokens[identity])
		}
		resp, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		raw, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatal(err)
		}
		var value map[string]any
		if err := json.Unmarshal(raw, &value); err != nil {
			t.Fatal("invalid JSON response", resp.StatusCode, string(raw))
		}
		return resp.StatusCode, value, string(raw)
	}
	check := func(method, path, identity, body string, want common.ErrorCode) map[string]any {
		t.Helper()
		status, value, raw := request(method, path, identity, body)
		if status != 200 || value["code"] != float64(want) {
			t.Fatalf("%s %s status %d: %s", method, path, status, raw)
		}
		return value
	}
	t.Run("CredentialIdentityMigrationMatrix", func(t *testing.T) {
		execSQL("DROP INDEX idx_tenant_model_instance_provider_name")
		execSQL("CREATE UNIQUE INDEX idx_tenant_model_instance_api_key_nonempty ON tenant_model_instance(api_key) WHERE api_key <> ''")
		execSQL("CREATE UNIQUE INDEX idx_tenant_model_instance_api_key ON tenant_model_instance(api_key)")
		for _, row := range []entity.TenantModelInstance{{ID: "g-legacy-a", ProviderID: "g-legacy-provider", InstanceName: "same", APIKey: "legacy-a", Status: "inactive", Extra: `{"base_url":"http://127.0.0.1:1"}`}, {ID: "g-legacy-b", ProviderID: "g-legacy-provider", InstanceName: "same", APIKey: "legacy-b", Status: "active", Extra: `{"base_url":"http://127.0.0.1:2"}`}} {
			if err := dao.DB.Create(&row).Error; err != nil {
				t.Fatal(err)
			}
		}
		before := snapshot("tenant_model_instance")
		if err := dao.RunMigrations(dao.DB); err == nil || !strings.Contains(err.Error(), "duplicate model instance names") {
			t.Fatal("ambiguous old identities not diagnosed", err)
		}
		if snapshot("tenant_model_instance") != before || !dao.DB.Migrator().HasIndex(&entity.TenantModelInstance{}, "idx_tenant_model_instance_api_key") || dao.DB.Migrator().HasIndex(&entity.TenantModelInstance{}, "idx_tenant_model_instance_provider_name") {
			t.Fatal("conflicting migration changed data or indexes")
		}
		execSQL("DELETE FROM tenant_model_instance WHERE id='g-legacy-b'")
		before = snapshot("tenant_model_instance")
		for i := 0; i < 2; i++ {
			if err := dao.RunMigrations(dao.DB); err != nil {
				t.Fatal("old upgrade/idempotence", err)
			}
		}
		if snapshot("tenant_model_instance") != before || dao.DB.Migrator().HasIndex(&entity.TenantModelInstance{}, "idx_tenant_model_instance_api_key") || dao.DB.Migrator().HasIndex(&entity.TenantModelInstance{}, "idx_tenant_model_instance_api_key_nonempty") {
			t.Fatal("migration did not preserve rows/remove credential uniqueness")
		}
		if err := dao.DB.Create(&entity.TenantModelInstance{ID: "g-legacy-shared", ProviderID: "g-legacy-provider", InstanceName: "other", APIKey: "legacy-a", Status: "active"}).Error; err != nil {
			t.Fatal("same key distinct identity refused", err)
		}
		before = snapshot("tenant_model_instance")
		if err := dao.DB.Create(&entity.TenantModelInstance{ID: "g-legacy-duplicate", ProviderID: "g-legacy-provider", InstanceName: "other", APIKey: "another-key", Status: "active"}).Error; err == nil {
			t.Fatal("duplicate identity accepted")
		}
		if snapshot("tenant_model_instance") != before {
			t.Fatal("identity rejection mutated rows")
		}
	})
	t.Run("JWT", func(t *testing.T) {
		for _, path := range []string{"/api/v1/agents", "/api/v1/system/status", "/api/v1/tenants/g-owner/users"} {
			status, _, _ := request("GET", path, "", "")
			if status != 401 {
				t.Fatal("missing JWT accepted", path)
			}
		}
	})
	t.Run("MetadataAuthorizationAndZeroWrites", func(t *testing.T) {
		for _, permission := range []string{"team", "me"} {
			id := "g-kb-" + permission
			kb := entity.Knowledgebase{ID: id, TenantID: "g-owner", CreatedBy: "g-owner", Name: id, Permission: permission, Status: &active, ParserConfig: entity.JSONMap{"keep": "yes"}}
			if err := dao.DB.Create(&kb).Error; err != nil {
				t.Fatal(err)
			}
		}
		for _, identity := range []string{"g-other", "g-invite", "g-inactive"} {
			before := snapshot("t_ai_knowledgebases")
			check("POST", "/v1/kb/update_metadata_setting", identity, `{"kb_id":"g-kb-team","metadata":{"x":1}}`, common.CodeAuthenticationError)
			if snapshot("t_ai_knowledgebases") != before {
				t.Fatal("denial wrote dataset")
			}
		}
		for _, body := range []string{`null`, `[]`, `{"kb_id":"g-kb-team","metadata":null}`, `{"kb_id":"g-kb-team","kb_id":"g-kb-me","metadata":{}}`, `{"kb_id":"g-kb-team","metadata":[],"enable_metadata":true}`, `{"kb_id":"g-kb-team","metadata":{},"enable_metadata":"false"}`} {
			before := snapshot("t_ai_knowledgebases")
			_, value, _ := request("POST", "/v1/kb/update_metadata_setting", "g-owner", body)
			if value["code"] == float64(0) || snapshot("t_ai_knowledgebases") != before {
				t.Fatal("invalid body accepted or wrote")
			}
		}
		before := snapshot("t_ai_knowledgebases")
		check("POST", "/v1/kb/update_metadata_setting", "g-member", `{"kb_id":"g-kb-me","metadata":{}}`, common.CodeAuthenticationError)
		check("POST", "/v1/kb/update_metadata_setting", "g-owner", `{"kb_id":"unknown","metadata":{}}`, common.CodeAuthenticationError)
		if before != snapshot("t_ai_knowledgebases") {
			t.Fatal("private/missing denial wrote")
		}
		check("POST", "/v1/kb/update_metadata_setting", "g-member", `{"kb_id":"g-kb-team","metadata":{},"enable_metadata":false}`, common.CodeSuccess)
		var enabled bool
		var metadata string
		if err := read.QueryRow("SELECT (parser_config->>'enable_metadata')::boolean,(parser_config->'metadata')::text FROM t_ai_knowledgebases WHERE id='g-kb-team'").Scan(&enabled, &metadata); err != nil || enabled || metadata != "{}" {
			t.Fatal("metadata zero/false persistence", err)
		}
		execSQL("ALTER TABLE t_ai_knowledgebases ADD CONSTRAINT g_reject_metadata CHECK (NOT parser_config ? 'enable_metadata' OR parser_config->>'enable_metadata' = 'false')")
		before = snapshot("t_ai_knowledgebases")
		check("POST", "/v1/kb/update_metadata_setting", "g-owner", `{"kb_id":"g-kb-team","metadata":{"rejected":1}}`, common.CodeServerError)
		if before != snapshot("t_ai_knowledgebases") {
			t.Fatal("failed write changed dataset")
		}
		execSQL("ALTER TABLE t_ai_knowledgebases DROP CONSTRAINT g_reject_metadata")
	})
	t.Run("ConnectorsExistingContract", func(t *testing.T) {
		value := check("POST", "/api/v1/connectors/", "g-owner", `{"name":"owned connector","source":"rest_api","config":{"credentials":{"key":"source-secret"}},"refresh_freq":0,"prune_freq":0}`, 0)
		id := value["data"].(map[string]any)["id"].(string)
		_, _, raw := request("GET", "/api/v1/connectors/"+id, "g-member", "")
		if strings.Contains(raw, "source-secret") || strings.Contains(raw, "credentials") {
			t.Fatal("connector config leaked")
		}
		value = check("GET", "/api/v1/connectors/"+id+"/logs", "g-owner", "", 0)
		data := value["data"].(map[string]any)
		if data["total"] != float64(0) || len(data["logs"].([]any)) != 0 {
			t.Fatal("empty log contract", data)
		}
		before := snapshot("t_ai_connectors")
		check("DELETE", "/api/v1/connectors/"+id, "g-other", "", common.CodeAuthenticationError)
		check("POST", "/api/v1/connectors/"+id+"/rebuild", "g-invite", `{"kb_id":"g-kb-team"}`, common.CodeAuthenticationError)
		if before != snapshot("t_ai_connectors") {
			t.Fatal("denial changed connectors")
		}
		if err := dao.DB.Create(&entity.Connector2Kb{ID: "g-binding", ConnectorID: id, KbID: "g-kb-team"}).Error; err != nil {
			t.Fatal(err)
		}
		check("POST", "/api/v1/connectors/"+id+"/rebuild", "g-owner", `{"kb_id":"g-kb-team"}`, 0)
		var n int
		if err := read.QueryRow("SELECT count(*) FROM t_ai_sync_logs WHERE connector_id=$1 AND status='5'", id).Scan(&n); err != nil || n != 1 {
			t.Fatal("rebuild schedule", n, err)
		}
		check("DELETE", "/api/v1/connectors/"+id, "g-owner", "", 0)
		if err := read.QueryRow("SELECT count(*) FROM t_ai_connectors WHERE id=$1", id).Scan(&n); err != nil || n != 0 {
			t.Fatal("delete readback", n, err)
		}
	})
	t.Run("AgentsVisibilityPagination", func(t *testing.T) {
		for _, item := range []struct{ id, owner, permission, category string }{{"g-a", "g-owner", "team", "agent_canvas"}, {"g-b", "g-owner", "me", "agent_canvas"}, {"g-c", "g-member", "me", "agent_canvas"}, {"g-d", "g-owner", "team", "dataflow_canvas"}, {"g-e", "g-other", "team", "agent_canvas"}} {
			title := item.id
			if err := dao.DB.Create(&entity.UserCanvas{ID: item.id, UserID: item.owner, Title: &title, Permission: item.permission, CanvasCategory: item.category, DSL: entity.JSONMap{}}).Error; err != nil {
				t.Fatal(err)
			}
		}
		value := check("GET", "/api/v1/agents?page=1&page_size=1&orderby=id&desc=false", "g-member", "", common.CodeSuccess)
		data := value["data"].(map[string]any)
		if data["total"] != float64(2) || len(data["canvas"].([]any)) != 1 {
			t.Fatal("wrong list total/page", data)
		}
		value = check("GET", "/api/v1/agents?owner_ids=g-owner", "g-member", "", 0)
		if value["data"].(map[string]any)["total"] != float64(1) {
			t.Fatal("owner filter includes own canvas")
		}
		value = check("GET", "/api/v1/agents?owner_ids=", "g-member", "", 0)
		if value["data"].(map[string]any)["total"] != float64(0) {
			t.Fatal("explicit empty broadens query")
		}
		for _, identity := range []string{"g-invite", "g-inactive"} {
			value = check("GET", "/api/v1/agents", identity, "", 0)
			if value["data"].(map[string]any)["total"] != float64(0) {
				t.Fatal("inactive invitation reads agents")
			}
		}
		for _, query := range []string{"page=abc", "page=-1&page_size=1", "page=1", "page=1&page=2", "page_size=999999999999999999999", "page=9223372036854775807&page_size=2", "desc=no", "orderby=id%3BDROP%20TABLE%20x", "owner_ids=g-owner,g-owner", "owner_ids=g-owner,", "canvas_category=bad"} {
			_, value, _ = request("GET", "/api/v1/agents?"+query, "g-member", "")
			if value["code"] == float64(0) {
				t.Fatal("invalid query accepted", query)
			}
		}
		check("GET", "/api/v1/agents?owner_ids=g-other", "g-member", "", common.CodeOperatingError)
	})
	t.Run("PendingInviteOldConsumers", func(t *testing.T) {
		metadata := entity.JSONMap{}
		permissionSnapshot := func() string {
			return snapshot("t_ai_user_tenants") + snapshot("t_ai_knowledgebases") + snapshot("t_ai_documents") + snapshot("t_ai_dialogs") + snapshot("t_ai_search")
		}
		if err := dao.DB.Create(&entity.Chat{ID: "g-shared-chat", TenantID: "g-owner", LLMSetting: entity.JSONMap{}, PromptConfig: entity.JSONMap{}, KBIDs: entity.JSONSlice{}, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := dao.DB.Create(&entity.Search{ID: "g-shared-search", TenantID: "g-owner", CreatedBy: "g-owner", Name: "owned", SearchConfig: entity.JSONMap{}, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := dao.DB.Create(&entity.Document{ID: "g-shared-doc", KbID: "g-kb-team", Name: utility.StringPtr("owned"), ParserID: "naive", ParserConfig: entity.JSONMap{}, Type: "txt", CreatedBy: "g-owner", Suffix: "txt", MetaFields: &metadata, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		paths := []struct{ method, path, body string }{{"GET", "/api/v1/chats/g-shared-chat", ""}, {"GET", "/api/v1/searches/g-shared-search", ""}, {"POST", "/v1/chunk/list", `{"doc_id":"g-shared-doc"}`}}
		before := permissionSnapshot()
		calls := readEngine.calls.Load()
		for _, req := range paths {
			status, value, _ := request(req.method, req.path, "g-invite", req.body)
			t.Log("permission-denied", req.path, "http", status, "code", value["code"], "engine-calls", readEngine.calls.Load())
			if value["code"] == float64(0) {
				t.Fatal("pending invitation reads old route", req.path)
			}
		}
		if permissionSnapshot() != before || readEngine.calls.Load() != calls {
			t.Fatal("pending denial writes SQL or reads engine")
		}
		check("PATCH", "/api/v1/tenants/g-owner", "g-invite", "", 0)
		acceptedBefore := snapshot("t_ai_user_tenants")
		privateBefore := snapshot("t_ai_knowledgebases")
		callsBefore := readEngine.calls.Load()
		check("POST", "/v1/kb/update_metadata_setting", "g-invite", `{"kb_id":"g-kb-me","metadata":{}}`, common.CodeAuthenticationError)
		if snapshot("t_ai_user_tenants") != acceptedBefore || snapshot("t_ai_knowledgebases") != privateBefore || readEngine.calls.Load() != callsBefore {
			t.Fatal("accepted private denial mutated resources")
		}

		for _, req := range paths {
			value := check(req.method, req.path, "g-invite", req.body, 0)
			t.Log("permission-accepted", req.path, "code", value["code"], "engine-calls", readEngine.calls.Load())
		}
		if readEngine.calls.Load() != calls+1 {
			t.Fatal("accepted chunk read not scoped")
		}
		check("DELETE", "/api/v1/tenants/g-owner/users", "g-invite", `{"user_id":"g-invite"}`, 0)
		before = permissionSnapshot()
		calls = readEngine.calls.Load()
		for _, req := range paths {
			status, value, _ := request(req.method, req.path, "g-invite", req.body)
			t.Log("permission-denied", req.path, "http", status, "code", value["code"], "engine-calls", readEngine.calls.Load())
			if value["code"] == float64(0) {
				t.Fatal("removed member reads old route", req.path)
			}
		}
		if permissionSnapshot() != before || readEngine.calls.Load() != calls {
			t.Fatal("revocation denial has forbidden side effect")
		}
	})
	t.Run("MemberLifecycleAndConcurrency", func(t *testing.T) {
		before := snapshot("t_ai_user_tenants")
		check("POST", "/api/v1/tenants/g-owner/users", "g-other", `{"email":"g-other@owned.invalid"}`, common.CodeAuthenticationError)
		if before != snapshot("t_ai_user_tenants") {
			t.Fatal("nonowner invited")
		}
		check("POST", "/api/v1/tenants/g-owner/users", "g-owner", `{"email":"g-other@owned.invalid"}`, 0)
		ids, err := dao.NewUserTenantDAO().GetTenantIDsByUserID("g-other")
		if err != nil {
			t.Fatal(err)
		}
		for _, id := range ids {
			if id == "g-owner" {
				t.Fatal("invite grants resources")
			}
		}
		before = snapshot("t_ai_user_tenants")
		check("PATCH", "/api/v1/tenants/g-owner", "g-member", "", common.CodeArgumentError)
		if before != snapshot("t_ai_user_tenants") {
			t.Fatal("noninvite accepted")
		}
		check("PATCH", "/api/v1/tenants/g-owner", "g-other", "", 0)
		check("DELETE", "/api/v1/tenants/g-owner/users", "g-other", `{"user_id":"g-member"}`, common.CodeAuthenticationError)
		check("DELETE", "/api/v1/tenants/g-owner/users", "g-other", `{"user_id":"g-other"}`, 0)
		check("DELETE", "/api/v1/tenants/g-owner/users", "g-owner", `{"user_id":"g-owner"}`, common.CodeArgumentError)
		var wg sync.WaitGroup
		successes := make(chan bool, 2)
		for i := 0; i < 2; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				_, code, err := ts.AddMember(context.Background(), "g-owner", "g-owner", &service.AddMemberRequest{Email: "g-other@owned.invalid"})
				successes <- err == nil && code == 0
			}()
		}
		wg.Wait()
		close(successes)
		success := 0
		for ok := range successes {
			if ok {
				success++
			}
		}
		if success != 1 {
			t.Fatal("concurrent duplicate invitations", success)
		}
		check("GET", "/api/v1/tenants/g-owner/users", "g-owner", "", 0)
		var n int
		if err := read.QueryRow("SELECT count(*) FROM t_ai_user_tenants WHERE tenant_id='g-owner' AND user_id='g-other' AND status='1'").Scan(&n); err != nil || n != 1 {
			t.Fatal("duplicate rows", n, err)
		}
	})
	t.Run("AdminUserTokenOwnedTenant", func(t *testing.T) {
		adminService := admin.NewService()
		value, err := adminService.GenerateUserAPIToken("g-member@owned.invalid")
		if err != nil {
			t.Fatal(err)
		}
		if value["tenant_id"] != "g-member" {
			t.Fatal("admin username token selected joined tenant")
		}
		before := snapshot("t_ai_api_tokens")
		if err := adminService.DeleteUserAPIToken("g-owner@owned.invalid", value["token"].(string)); err == nil {
			t.Fatal("other username deleted token")
		}
		if snapshot("t_ai_api_tokens") != before {
			t.Fatal("admin token denial wrote")
		}
		if err := adminService.DeleteUserAPIToken("g-member@owned.invalid", value["token"].(string)); err != nil {
			t.Fatal(err)
		}
	})
	t.Run("MCPDeleteReferencesAndSQLFailure", func(t *testing.T) {
		value := check("POST", "/api/v1/mcp/servers", "g-owner", `{"name":"owned MCP","url":"https://example.invalid/mcp","server_type":"sse","headers":{"Authorization":"secret-header"}}`, 0)
		id := value["data"].(map[string]any)["id"].(string)
		before := snapshot("t_ai_mcp_server")
		check("DELETE", "/api/v1/mcp/servers/"+id, "g-other", "", common.CodeDataError)
		if before != snapshot("t_ai_mcp_server") {
			t.Fatal("cross tenant delete wrote")
		}
		execSQL("UPDATE t_ai_user_canvases SET dsl=jsonb_build_object('tools',jsonb_build_array(jsonb_build_object('mcp_id',$1::text))) WHERE id='g-a'", id)
		check("DELETE", "/api/v1/mcp/servers/"+id, "g-owner", "", common.CodeOperatingError)
		if before != snapshot("t_ai_mcp_server") {
			t.Fatal("reference rejection deleted")
		}
		execSQL("UPDATE t_ai_user_canvases SET dsl='{}' WHERE id='g-a'")
		execSQL("CREATE TABLE g_mcp_ref (mcp_id text REFERENCES t_ai_mcp_server(id))")
		execSQL("INSERT INTO g_mcp_ref VALUES ($1)", id)
		check("DELETE", "/api/v1/mcp/servers/"+id, "g-owner", "", common.CodeServerError)
		if before != snapshot("t_ai_mcp_server") {
			t.Fatal("SQL failure deleted")
		}
		execSQL("DROP TABLE g_mcp_ref")
		check("DELETE", "/api/v1/mcp/servers/"+id, "g-owner", "", 0)
		if count("t_ai_mcp_server") != 0 {
			t.Fatal("delete not committed")
		}
		check("DELETE", "/api/v1/mcp/servers/"+id, "g-owner", "", common.CodeDataError)
	})
	t.Run("SystemStatusConfigLogsAndCLI", func(t *testing.T) {
		value := check("GET", "/api/v1/system/status", "g-owner", "", 0)
		data := value["data"].(map[string]any)
		if data["database"].(map[string]any)["status"] != "green" || data["doc_engine"].(map[string]any)["status"] != "red" || data["storage"].(map[string]any)["status"] != "red" || data["task_executor_heartbeat_error"] == nil {
			t.Fatal("fake health/worker state", data)
		}
		_, _, raw := request("GET", "/api/v1/system/configs", "g-owner", "")
		for _, secret := range []string{cfg.Database.Password, cfg.Server.SecretKey, "APIKey", "Password", "SecretKey"} {
			if secret != "" && strings.Contains(raw, secret) {
				t.Fatal("config exposes secret")
			}
		}
		check("PUT", "/api/v1/system/log", "g-owner", `{"level":"debug","pkg_name":"root"}`, 0)
		if common.GetLevel() != "debug" {
			t.Fatal("log setter did not apply")
		}
		check("PUT", "/api/v1/system/log", "g-owner", `{"level":"error","pkg_name":"peewee"}`, common.CodeArgumentError)
		if common.GetLevel() != "debug" {
			t.Fatal("unsupported package changes root")
		}
		check("PUT", "/api/v1/system/log", "g-owner", `{"level":"error"}`, 0)
		binary := os.Getenv("MULTIRAG_BATCH_G_CLI")
		if binary == "" {
			t.Fatal("fresh CLI required")
		}
		parsed, _ := url.Parse(api.URL)
		for _, syntax := range []string{"list configs;", "set log level error;"} {
			cmd := exec.CommandContext(t.Context(), binary, "-f", filepath.Join(filepath.Dir(config), "cli.json"), "-h", parsed.Host, "-t", tokens["g-owner"], "-o", "json", syntax)
			output, err := cmd.CombinedOutput()
			if err != nil || bytes.Contains(output, []byte(cfg.Database.Password)) {
				t.Fatalf("CLI %s failed: %v %s", syntax, err, output)
			}
			t.Log("fresh CLI exit", cmd.ProcessState.ExitCode(), syntax)
		}
	})
	t.Run("StatsTenantSourceAndDates", func(t *testing.T) {
		for _, owner := range []string{"g-owner", "g-other"} {
			chat := entity.Chat{ID: owner, TenantID: owner, LLMID: "", LLMSetting: entity.JSONMap{}, PromptConfig: entity.JSONMap{}, KBIDs: entity.JSONSlice{}}
			if err := dao.DB.Create(&chat).Error; err != nil {
				t.Fatal(err)
			}
		}
		day := time.Date(2026, 10, 20, 12, 0, 0, 0, time.UTC)
		agent := "agent"
		for _, row := range []entity.API4Conversation{{ID: "g-stat-1", DialogID: "g-owner", UserID: "u1", Tokens: 1000, Duration: 0, BaseModel: entity.BaseModel{CreateDate: &day}}, {ID: "g-stat-2", DialogID: "g-other", UserID: "u2", Tokens: 9000, BaseModel: entity.BaseModel{CreateDate: &day}}, {ID: "g-stat-a", DialogID: "g-a", UserID: "u1", Source: &agent, Tokens: 2000, Duration: 2, BaseModel: entity.BaseModel{CreateDate: &day}}} {
			if err := dao.DB.Create(&row).Error; err != nil {
				t.Fatal(err)
			}
		}
		value := check("GET", "/api/v1/system/stats?from_date=2026-10-20&to_date=2026-10-20", "g-owner", "", 0)
		data := value["data"].(map[string]any)
		if data["pv"].([]any)[0].([]any)[1] != float64(1) || data["tokens"].([]any)[0].([]any)[1] != float64(1) || data["speed"].([]any)[0].([]any)[1] != float64(0) {
			t.Fatal("tenant or zero stats", data)
		}
		value = check("GET", "/api/v1/system/stats?from_date=2026-10-20&to_date=2026-10-20&canvas_id=g-a", "g-member", "", 0)
		if value["data"].(map[string]any)["tokens"].([]any)[0].([]any)[1] != float64(2) {
			t.Fatal("agent stats missing")
		}
		check("GET", "/api/v1/system/stats?canvas_id=g-a", "g-other", "", common.CodeAuthenticationError)
		check("GET", "/api/v1/system/stats?from_date=bad", "g-owner", "", common.CodeArgumentError)
		check("GET", "/api/v1/system/stats?canvas_id=&canvas_id=g-a", "g-owner", "", common.CodeArgumentError)
	})
	t.Run("RegistrationAtomicDefaults", func(t *testing.T) {
		keyData, err := os.ReadFile("configs/private.pem")
		if err != nil {
			t.Fatal(err)
		}
		block, _ := pem.Decode(keyData)
		var der []byte
		if block.Headers["Proc-Type"] != "" {
			der, err = x509.DecryptPEMBlock(block, []byte("Welcome"))
		} else {
			der = block.Bytes
		}
		if err != nil {
			t.Fatal(err)
		}
		key, err := x509.ParsePKCS1PrivateKey(der)
		if err != nil {
			t.Fatal(err)
		}
		encrypted, err := rsa.EncryptPKCS1v15(rand.Reader, &key.PublicKey, []byte(base64.StdEncoding.EncodeToString([]byte("owned-test-password"))))
		if err != nil {
			t.Fatal(err)
		}
		tables := []string{"t_ai_users", "t_ai_tenants", "t_ai_user_tenants", "t_ai_files", "tenant_model_provider", "tenant_model_instance", "tenant_model"}
		before := map[string]int{}
		for _, table := range tables {
			before[table] = count(table)
		}
		cfg.RegisterEnabled = 1
		modelHTTP := newOwnedOptInHTTPServer(t, "registration-model", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Header.Get("Authorization") != "Bearer owned-default-secret" {
				t.Error("wrong default model credentials")
				w.WriteHeader(401)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			switch r.URL.Path {
			case "/v1/chat/completions":
				io.WriteString(w, `{"id":"owned","object":"chat.completion","choices":[{"index":0,"message":{"role":"assistant","content":"default works"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}`)
			case "/v1/embeddings":
				io.WriteString(w, `{"object":"list","data":[{"object":"embedding","index":0,"embedding":[0,1,2]}],"model":"text-embedding-3-small","usage":{"prompt_tokens":1,"total_tokens":1}}`)
			default:
				w.WriteHeader(404)
			}
		}))
		cfg.UserDefaultLLM.DefaultModels.ChatModel = server.ModelConfig{Name: "gpt-4o", Factory: "OpenAI", APIKey: "owned-default-secret", BaseURL: modelHTTP.URL + "/v1"}
		embeddingHTTP := newOwnedOptInHTTPServer(t, "registration-embedding", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Header.Get("Authorization") != "Bearer owned-default-secret" || r.URL.Path != "/v1/embeddings" {
				t.Error("default embedding selected wrong endpoint/credential")
				w.WriteHeader(400)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			io.WriteString(w, `{"object":"list","data":[{"object":"embedding","index":0,"embedding":[0,1,2]}],"model":"text-embedding-3-small","usage":{"prompt_tokens":1,"total_tokens":1}}`)
		}))
		cfg.UserDefaultLLM.DefaultModels.EmbeddingModel = server.ModelConfig{Name: "text-embedding-3-small", Factory: "OpenAI", APIKey: "owned-default-secret", BaseURL: embeddingHTTP.URL + "/v1"}
		execSQL("ALTER TABLE t_ai_files ADD CONSTRAINT g_reject_root CHECK (name != '/')")
		_, code, err := us.Register(&service.RegisterRequest{Email: "rollback@owned.invalid", Nickname: "rollback", Password: base64.StdEncoding.EncodeToString(encrypted)})
		if err == nil || code != common.CodeServerError {
			t.Fatal("registration SQL failure accepted")
		}
		for _, table := range tables {
			if count(table) != before[table] {
				t.Fatal("partial account persisted", table)
			}
		}
		execSQL("ALTER TABLE t_ai_files DROP CONSTRAINT g_reject_root")
		req := fmt.Sprintf(`{"email":"created@owned.invalid","nickname":"created","password":%q}`, base64.StdEncoding.EncodeToString(encrypted))
		value := check("POST", "/api/v1/users", "", req, 0)
		_ = value
		var defaultRef, extra, apiKey string
		if err := read.QueryRow("SELECT t.llm_id,i.extra,i.api_key FROM t_ai_tenants t JOIN tenant_model_provider p ON p.tenant_id=t.id JOIN tenant_model_instance i ON i.provider_id=p.id JOIN t_ai_users u ON u.id=t.id WHERE u.email='created@owned.invalid' AND i.instance_name='default-chat'").Scan(&defaultRef, &extra, &apiKey); err != nil || defaultRef != "gpt-4o@default-chat@OpenAI" || apiKey != "owned-default-secret" || !strings.Contains(extra, modelHTTP.URL) {
			t.Fatal("default initialization invalid", err)
		}
		if count("tenant_model") != before["tenant_model"]+2 {
			t.Fatal("default model row missing")
		}
		reqSecond := fmt.Sprintf(`{"email":"second@owned.invalid","nickname":"second","password":%q}`, base64.StdEncoding.EncodeToString(encrypted))
		check("POST", "/api/v1/users", "", reqSecond, 0)
		var createdID string
		if err := read.QueryRow("SELECT id FROM t_ai_users WHERE email='created@owned.invalid'").Scan(&createdID); err != nil {
			t.Fatal(err)
		}
		resolver := service.NewModelProviderService()
		chat, err := resolver.GetChatModel(createdID, "")
		if err != nil {
			t.Fatal("registered default chat cannot resolve", err)
		}
		answer, err := chat.Chat("", []map[string]string{{"role": "user", "content": "hello"}}, nil)
		if err != nil || answer != "default works" {
			t.Fatal("registered default chat cannot run", err, answer)
		}
		var secondID string
		if err := read.QueryRow("SELECT id FROM t_ai_users WHERE email='second@owned.invalid'").Scan(&secondID); err != nil {
			t.Fatal(err)
		}
		secondChat, err := resolver.GetChatModel(secondID, "")
		if err != nil {
			t.Fatal("second owner same-key default did not resolve", err)
		}
		if answer, err := secondChat.Chat("", []map[string]string{{"role": "user", "content": "hello"}}, nil); err != nil || answer != "default works" {
			t.Fatal("second owner same-key default cannot run", err)
		}
		embedding, err := resolver.GetEmbeddingModel(createdID, "")
		if err != nil {
			t.Fatal("registered default embedding cannot resolve", err)
		}
		vectors, err := embedding.Embed([]string{"hello"})
		if err != nil || len(vectors) != 1 {
			t.Fatal("registered default embedding cannot run", err)
		}
		for _, table := range tables {
			before[table] = count(table)
		}
		check("POST", "/api/v1/users", "", `{"email":"bad@owned.invalid","nickname":"bad","password":"invalid"}`, common.CodeServerError)
		for _, table := range tables {
			if count(table) != before[table] {
				t.Fatal("decrypt failure wrote", table)
			}
		}
	})
	t.Log("JWT/metadata/agents/member/MCP/system/stats/registration acceptance completed; scratch DB removal independently verified by outer harness")
}
