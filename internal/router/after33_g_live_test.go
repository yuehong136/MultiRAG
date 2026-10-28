package router

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
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
	"github.com/google/uuid"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/testsupport"
	"multirag/internal/utility"
)

// TestAfter33GManagementHTTPAndSQL uses the registered routes, JWTs, TCP SQL,
// an independent SQL reader and a controlled provider. It performs no real sync.
func TestAfter33GManagementHTTPAndSQL(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_AFTER33_G_DSN")
	if dsn == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned scratch SQL required")
		}
		t.Skip("requires owned scratch SQL")
	}
	u, err := url.Parse(dsn)
	if err != nil || u.Hostname() != "127.0.0.1" || !strings.HasPrefix(strings.TrimPrefix(u.Path, "/"), "multirag_after33_g_") {
		t.Fatal("owned TCP SQL required")
	}
	j := testsupport.NewCase(t)
	cfg := filepath.Join(os.Getenv("TMPDIR"), "after33-g.yaml")
	j.Event("reserve-config", map[string]any{"path": cfg})
	if os.WriteFile(cfg, []byte("multirag:\n  secret_key: after33-g-owned-secret-32-characters\n"), 0600) != nil {
		t.Fatal("config write")
	}
	if err := server.FromConfigFile(cfg); err != nil {
		t.Fatal("config load")
	}
	if err := server.InitVariables(nil); err != nil {
		t.Fatal("init variables")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("SQL connect")
	}
	pool, _ := db.DB()
	t.Cleanup(func() { pool.Close() })
	reader, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("reader connect")
	}
	t.Cleanup(func() { reader.Close() })
	previous := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = previous })
	tables := []string{"t_ai_users", "t_ai_tenants", "t_ai_user_tenants", "t_ai_knowledgebases", "t_ai_mcp_server", "t_ai_connectors", "t_ai_connector2kb", "t_ai_sync_logs", "t_ai_api_tokens", "tenant_model_provider", "tenant_model_instance", "tenant_model"}
	j.Event("reserve-sql-schema", map[string]any{"database": u.Path, "tables": tables})
	if err := db.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.Knowledgebase{}, &entity.MCPServer{}, &entity.Connector{}, &entity.Connector2Kb{}, &entity.SyncLogs{}, &entity.APIToken{}, &entity.TenantModelProvider{}, &entity.TenantModelInstance{}, &entity.TenantModel{}); err != nil {
		t.Fatal("schema", err)
	}
	kbID := strings.ReplaceAll(uuid.Must(uuid.NewUUID()).String(), "-", "")
	foreignKB := strings.ReplaceAll(uuid.Must(uuid.NewUUID()).String(), "-", "")
	j.Event("reserve-sql-identities", map[string]any{"users": []string{"owner", "member", "foreign", "revoked"}, "datasets": []string{kbID, foreignKB}, "mcp": []string{"one", "two", "foreign"}, "connectors": []string{"owned", "foreign", "broken"}, "links": []string{"owned-link", "broken-link"}, "syncLogs": []string{"done"}, "dynamicIDs": "generated API token and connector schedule UUIDs only in owned database"})
	active, inactive, session := "1", "0", "owned-session"
	for _, id := range []string{"owner", "member", "foreign", "revoked"} {
		if err := db.Create(&entity.User{ID: id, Email: id + "@after33.invalid", Nickname: id, Status: &active, AccessToken: &session}).Error; err != nil {
			t.Fatal("seed user")
		}
		tenant, role, status := "owner", "normal", active
		if id == "owner" || id == "foreign" {
			tenant = id
			role = "owner"
		}
		if id == "revoked" {
			status = inactive
		}
		if err := db.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: tenant, Role: role, Status: &status}).Error; err != nil {
			t.Fatal("seed membership")
		}
	}
	for _, id := range []string{"owner", "foreign"} {
		if err := db.Create(&entity.Tenant{ID: id, Status: &active}).Error; err != nil {
			t.Fatal("seed tenant")
		}
	}
	for _, id := range []string{kbID, foreignKB} {
		tenant := "owner"
		if id == foreignKB {
			tenant = "foreign"
		}
		if err := db.Create(&entity.Knowledgebase{ID: id, TenantID: tenant, CreatedBy: tenant, Name: id, Permission: "team", Status: &active, EmbdID: "none", ParserConfig: entity.JSONMap{"chunk_token_num": 128, "metadata": []any{map[string]any{"key": "old"}}, "built_in_metadata": []any{map[string]any{"key": "source"}}}}).Error; err != nil {
			t.Fatal("seed dataset")
		}
	}
	for _, id := range []string{"one", "two", "foreign"} {
		tenant := "owner"
		if id == "foreign" {
			tenant = id
		}
		if err := db.Create(&entity.MCPServer{ID: id, TenantID: tenant, Name: id, URL: "https://example.invalid/mcp?token=secret-query", ServerType: "sse", Headers: entity.JSONMap{"Authorization": "secret-header"}, Variables: entity.JSONMap{"authorization_token": "secret-variable", "tools": map[string]any{"secret-tool": map[string]any{"key": "secret-nested"}}}}).Error; err != nil {
			t.Fatal("seed MCP")
		}
	}
	for _, id := range []string{"owned", "foreign", "broken"} {
		tenant := "owner"
		if id == "foreign" {
			tenant = id
		}
		if err := db.Create(&entity.Connector{ID: id, TenantID: tenant, Name: id, Source: "rss", InputType: "poll", Config: entity.JSONMap{"api_key": "secret-connector", "sync_deleted_files": true}, RefreshFreq: 5, PruneFreq: 5, TimeoutSecs: 120, Status: "0"}).Error; err != nil {
			t.Fatal("seed connector")
		}
	}
	for _, binding := range []entity.Connector2Kb{{ID: "owned-link", ConnectorID: "owned", KbID: kbID}, {ID: "broken-link", ConnectorID: "broken", KbID: foreignKB}} {
		if err := db.Create(&binding).Error; err != nil {
			t.Fatal("seed link")
		}
	}
	cursor := "cursor-42"
	if err := db.Create(&entity.SyncLogs{ID: "done", ConnectorID: "owned", KbID: kbID, TaskType: "sync", Status: "3", PollRangeEnd: &cursor, TotalDocsIndexed: 42, ErrorMsg: ""}).Error; err != nil {
		t.Fatal("seed cursor")
	}
	oldAdmin := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned")
	t.Cleanup(func() { local.SetAdminStatus(oldAdmin.Status, oldAdmin.Reason) })
	start := func(addr string, h http.Handler) *httptest.Server {
		t.Helper()
		j.Event("reserve-listener", map[string]any{"address": addr})
		l, err := net.Listen("tcp", addr)
		if err != nil {
			t.Fatal("bind owned listener")
		}
		s := &httptest.Server{Listener: l, Config: &http.Server{Handler: h}}
		s.Start()
		t.Cleanup(func() {
			s.Close()
			c, err := net.DialTimeout("tcp", addr, time.Second)
			if err == nil {
				c.Close()
				t.Error("listener survived")
			}
			j.Event("listener-cleanup-readback", map[string]any{"address": addr, "absent": err != nil})
		})
		return s
	}
	var calls atomic.Int64
	slowStarted := make(chan struct{}, 1)
	slowCancelled := make(chan struct{}, 1)
	provider := start("127.0.0.1:18932", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("Authorization") == "Bearer secret-slow" {
			slowStarted <- struct{}{}
			<-r.Context().Done()
			slowCancelled <- struct{}{}
			return
		}
		if r.URL.Path != "/v1/models" {
			t.Error("wrong provider endpoint")
			w.WriteHeader(404)
			return
		}
		if r.Header.Get("Authorization") == "Bearer secret-fail" {
			w.WriteHeader(401)
			fmt.Fprint(w, `{"error":"secret-provider-body"}`)
			return
		}
		if auth := r.Header.Get("Authorization"); auth != "Bearer secret-key" && auth != "" {
			t.Error("wrong key")
		}
		fmt.Fprint(w, `{"data":[{"id":"controlled-model"}]}`)
	}))
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal("catalog load")
	}
	modelService := service.NewModelProviderServiceWithCatalog(manager, nil)
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	(&Router{authHandler: handler.NewAuthHandler(), userHandler: handler.NewUserHandler(service.NewUserService()), systemHandler: handler.NewSystemHandler(service.NewSystemService()), documentHandler: handler.NewDocumentHandler(service.NewDocumentService(nil)), datasetsHandler: handler.NewDatasetsHandler(service.NewDatasetsService()), connectorHandler: handler.NewConnectorHandler(service.NewConnectorService(nil), service.NewUserService()), providerHandler: handler.NewProviderHandler(service.NewUserService(), modelService)}).Setup(app)
	api := start("127.0.0.1:18931", app)
	tokens := map[string]string{}
	for _, id := range []string{"owner", "member", "foreign", "revoked"} {
		tokens[id], err = utility.SignUserJWT(id+"@after33.invalid", server.GetSecretKey())
		if err != nil {
			t.Fatal("JWT")
		}
	}
	request := func(method, path, user, body string) (map[string]any, string) {
		t.Helper()
		req, _ := http.NewRequest(method, api.URL+path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		if user != "" {
			req.Header.Set("Authorization", "Bearer "+tokens[user])
		}
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal("HTTP request")
		}
		defer res.Body.Close()
		raw, _ := io.ReadAll(res.Body)
		if bytes.Contains(raw, []byte("secret-")) {
			t.Fatal("credential leaked")
		}
		var payload map[string]any
		if json.Unmarshal(raw, &payload) != nil {
			t.Fatal("invalid JSON response")
		}
		return payload, string(raw)
	}
	snapshot := func() string {
		t.Helper()
		var all []string
		for _, table := range tables {
			var raw string
			if err := reader.QueryRow("SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb)::text FROM " + table + " t").Scan(&raw); err != nil {
				t.Fatal("snapshot", table)
			}
			all = append(all, raw)
		}
		return strings.Join(all, "\n")
	}
	assertCode := func(payload map[string]any, success bool) {
		t.Helper()
		if (payload["code"] == float64(0)) != success {
			t.Fatalf("unexpected business code %v", payload["code"])
		}
	}
	t.Run("provider zero writes and denial zero calls", func(t *testing.T) {
		before := snapshot()
		base := provider.URL + "/v1"
		body := fmt.Sprintf(`{"api_key":"secret-key","region":"default","base_url":%q}`, base)
		for _, test := range []struct{ user, body string }{{"", body}, {"member", body}, {"revoked", body}, {"owner", `{"api_key":"secret-key","region":"missing"}`}, {"owner", `{"api_key":null}`}, {"owner", `{"api_key":"a","api_key":"b"}`}, {"owner", `{"api_key":"a","extra":true}`}, {"owner", `{"api_key":"secret-key","region":false}`}, {"owner", `{"api_key":"secret-key","base_url":"http://user:pass@example.invalid"}`}} {
			count := calls.Load()
			p, _ := request("GET", "/api/v1/providers/OpenAI/connection", test.user, test.body)
			assertCode(p, false)
			if calls.Load() != count {
				t.Fatal("denied request called provider")
			}
			if snapshot() != before {
				t.Fatal("denied request wrote SQL")
			}
		}
		p, _ := request("GET", "/api/v1/providers/OpenAI/connection", "owner", body)
		assertCode(p, true)
		p, _ = request("GET", "/api/v1/providers/OpenAI/connection", "owner", strings.Replace(body, "secret-key", "secret-fail", 1))
		assertCode(p, false)
		p, _ = request("GET", "/api/v1/providers/LM-Studio/connection", "owner", fmt.Sprintf(`{"api_key":"","base_url":%q}`, base))
		assertCode(p, true)
		if snapshot() != before {
			t.Fatal("provider check persisted data")
		}
	})
	t.Run("provider parent cancellation", func(t *testing.T) {
		before := snapshot()
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		body := fmt.Sprintf(`{"api_key":"secret-slow","base_url":%q}`, provider.URL+"/v1")
		req, _ := http.NewRequestWithContext(ctx, "GET", api.URL+"/api/v1/providers/OpenAI/connection", strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+tokens["owner"])
		req.Header.Set("Content-Type", "application/json")
		done := make(chan error, 1)
		go func() {
			response, err := http.DefaultClient.Do(req)
			if response != nil {
				response.Body.Close()
			}
			done <- err
		}()
		select {
		case <-slowStarted:
		case <-time.After(2 * time.Second):
			t.Fatal("provider probe did not start")
		}
		cancel()
		select {
		case <-slowCancelled:
		case <-time.After(2 * time.Second):
			t.Fatal("request cancellation did not reach provider")
		}
		if <-done == nil {
			t.Fatal("cancelled HTTP request succeeded")
		}
		if snapshot() != before {
			t.Fatal("cancelled probe wrote SQL")
		}
	})

	metadataPath := "/api/v1/datasets/" + kbID + "/metadata/config"
	t.Run("metadata patch semantics and permissions", func(t *testing.T) {
		before := snapshot()
		for _, test := range []struct{ user, body string }{{"member", `{"enabled":true}`}, {"revoked", `{"metadata":[]}`}, {"foreign", `{"metadata":[]}`}, {"owner", `{"metadata":null}`}, {"owner", `{"metadata":[{"key":"bad","type":"number","enum":[false]}]}`}, {"owner", `{"metadata":[{"key":"bad","type":"number","enum":["NaN"]}]}`}, {"owner", `{"metadata":[{"key":"bad","type":"list","items":42}]}`}, {"owner", `{"enabled":false,"enabled":true}`}} {
			p, _ := request("PUT", metadataPath, test.user, test.body)
			assertCode(p, false)
			if snapshot() != before {
				t.Fatal("invalid metadata wrote")
			}
		}
		p, _ := request("GET", metadataPath, "member", "")
		assertCode(p, true)
		p, _ = request("PUT", metadataPath, "owner", `{"enabled":true,"metadata":[{"name":"score","type":"number","enum":[0,"2",9007199254740993],"examples":[0],"extension":{"kept":true}},{"key":"labels","type":"list","items":false,"enum":["x"]}]}`)
		assertCode(p, true)
		var raw string
		if reader.QueryRow("SELECT parser_config::text FROM t_ai_knowledgebases WHERE id=$1", kbID).Scan(&raw) != nil {
			t.Fatal("metadata readback")
		}
		if !strings.Contains(raw, `"chunk_token_num": 128`) || !strings.Contains(raw, `9007199254740993`) || !strings.Contains(raw, `"extension"`) || !strings.Contains(raw, `"built_in_metadata"`) {
			t.Fatal("metadata erased fields")
		}
		p, _ = request("PUT", metadataPath, "owner", `{"enabled":false,"metadata":[]}`)
		assertCode(p, true)
		p, _ = request("GET", metadataPath, "owner", "")
		assertCode(p, true)
		data := p["data"].(map[string]any)
		if data["enabled"] != false || len(data["metadata"].([]any)) != 0 || len(data["built_in_metadata"].([]any)) != 1 {
			t.Fatal("metadata omission or false/clear lost")
		}
	})
	t.Run("MCP update secret preservation conflict and rollback", func(t *testing.T) {
		path := "/api/v1/mcp/servers/one"
		before := snapshot()
		for _, test := range []struct{ user, body string }{{"member", `{"name":"new"}`}, {"foreign", `{"headers":{}}`}, {"revoked", `{"name":"new"}`}, {"owner", `{"name":"two"}`}, {"owner", `{"headers":null}`}, {"owner", `{"headers":{"A":"bad\r\nvalue"}}`}, {"owner", `{"url":"file:///tmp/a"}`}, {"owner", `{"variables":false}`}, {"owner", `{"unknown":true}`}} {
			p, _ := request("PUT", path, test.user, test.body)
			assertCode(p, false)
			if snapshot() != before {
				t.Fatal("invalid MCP wrote")
			}
		}
		p, raw := request("PUT", path, "owner", `{"name":"renamed","description":null}`)
		assertCode(p, true)
		if !strings.Contains(raw, `"description":null`) {
			t.Fatal("nullable description lost")
		}
		var header, variable string
		if reader.QueryRow("SELECT headers::text, variables::text FROM t_ai_mcp_server WHERE id='one'").Scan(&header, &variable) != nil || !strings.Contains(header, "secret-header") || !strings.Contains(variable, "secret-variable") {
			t.Fatal("omitted MCP settings erased")
		}
		j.Event("reserve-sql-fault", map[string]any{"table": "t_ai_mcp_server", "constraint": "after33_mcp_fail"})
		if db.Exec("ALTER TABLE t_ai_mcp_server ADD CONSTRAINT after33_mcp_fail CHECK (name <> 'reject-write') NOT VALID").Error != nil {
			t.Fatal("MCP fault setup")
		}
		before = snapshot()
		p, _ = request("PUT", path, "owner", `{"name":"reject-write","headers":{"Authorization":"secret-new"}}`)
		assertCode(p, false)
		if snapshot() != before {
			t.Fatal("MCP SQL failure did not rollback")
		}
		if db.Exec("ALTER TABLE t_ai_mcp_server DROP CONSTRAINT after33_mcp_fail").Error != nil {
			t.Fatal("MCP fault cleanup")
		}

		var wg sync.WaitGroup
		codes := make(chan bool, 2)
		for _, id := range []string{"one", "two"} {
			wg.Add(1)
			go func(id string) {
				defer wg.Done()
				p, _ := request("PUT", "/api/v1/mcp/servers/"+id, "owner", `{"name":"same-name"}`)
				codes <- p["code"] == float64(0)
			}(id)
		}
		wg.Wait()
		close(codes)
		successes := 0
		for ok := range codes {
			if ok {
				successes++
			}
		}
		if successes != 1 {
			t.Fatal("duplicate name race")
		}
		p, _ = request("PUT", path, "owner", `{"headers":{},"variables":{}}`)
		assertCode(p, true)
		if reader.QueryRow("SELECT headers::text, variables::text FROM t_ai_mcp_server WHERE id='one'").Scan(&header, &variable) != nil || header != "{}" || strings.Contains(variable, "secret-") {
			t.Fatal("explicit clear lost")
		}
	})
	t.Run("connector atomic patch scheduling cursor and rollback", func(t *testing.T) {
		path := "/api/v1/connectors/owned"
		before := snapshot()
		for _, test := range []struct{ user, body string }{{"foreign", `{"refresh_freq":0}`}, {"revoked", `{"refresh_freq":0}`}, {"owner", `{"config":null}`}, {"owner", `{"timeout_secs":0}`}, {"owner", `{"prune_freq":-1}`}, {"owner", `{"status":"bad"}`}, {"owner", `{"reschedule":"false"}`}, {"owner", `{"status":"CANCEL","reschedule":true}`}, {"owner", `{"config":{"sync_deleted_files":"true"}}`}} {
			p, _ := request("PATCH", path, test.user, test.body)
			assertCode(p, false)
			if snapshot() != before {
				t.Fatal("invalid connector wrote")
			}
		}
		p, _ := request("PATCH", path, "member", `{"refresh_freq":0,"prune_freq":0,"reschedule":false}`)
		assertCode(p, true)
		var config string
		var refresh int64
		if reader.QueryRow("SELECT config::text,refresh_freq FROM t_ai_connectors WHERE id='owned'").Scan(&config, &refresh) != nil || !strings.Contains(config, "secret-connector") || refresh != 0 {
			t.Fatal("connector patch erased config or zero")
		}
		var wg sync.WaitGroup
		for range 2 {
			wg.Add(1)
			go func() {
				defer wg.Done()
				p, _ := request("PATCH", path, "owner", `{"status":"SCHEDULE"}`)
				assertCode(p, true)
			}()
		}
		wg.Wait()
		var count int
		if reader.QueryRow("SELECT count(*) FROM t_ai_sync_logs WHERE connector_id='owned' AND status='5'").Scan(&count) != nil || count != 2 {
			t.Fatal("concurrent schedule duplicated")
		}
		var cursor string
		var total int
		if reader.QueryRow("SELECT poll_range_start,total_docs_indexed FROM t_ai_sync_logs WHERE connector_id='owned' AND task_type='sync' AND status='5'").Scan(&cursor, &total) != nil || cursor != "cursor-42" || total != 42 {
			t.Fatal("resume cursor lost")
		}
		before = snapshot()
		p, _ = request("PATCH", "/api/v1/connectors/broken", "owner", `{"refresh_freq":17,"reschedule":true}`)
		assertCode(p, false)
		if snapshot() != before {
			t.Fatal("cross-tenant binding did not roll back")
		}
		j.Event("reserve-sql-fault", map[string]any{"table": "t_ai_sync_logs", "constraint": "after33_owned_fail"})
		if db.Exec("ALTER TABLE t_ai_sync_logs ADD CONSTRAINT after33_owned_fail CHECK (status <> '2') NOT VALID").Error != nil {
			t.Fatal("fault setup")
		}
		before = snapshot()
		p, _ = request("PATCH", path, "owner", `{"refresh_freq":19,"reschedule":true}`)
		assertCode(p, false)
		if snapshot() != before {
			t.Fatal("schedule SQL failure did not rollback config")
		}
		if db.Exec("ALTER TABLE t_ai_sync_logs DROP CONSTRAINT after33_owned_fail").Error != nil {
			t.Fatal("fault cleanup")
		}
		p, _ = request("PATCH", path, "owner", `{"status":"CANCEL","config":{}}`)
		assertCode(p, true)
		if reader.QueryRow("SELECT config::text FROM t_ai_connectors WHERE id='owned'").Scan(&config) != nil || config != "{}" {
			t.Fatal("explicit empty config not applied")
		}
	})
	t.Run("inactive tenant denies new management paths", func(t *testing.T) {
		j.Event("reserve-tenant-state-fixture", map[string]any{"tenant": "owner", "status": "0 then restored to 1"})
		if db.Model(&entity.Tenant{}).Where("id = ?", "owner").Update("status", "0").Error != nil {
			t.Fatal("disable tenant")
		}
		before := snapshot()
		count := calls.Load()
		for _, test := range []struct{ method, path, body string }{
			{"GET", "/api/v1/providers/OpenAI/connection", fmt.Sprintf(`{"api_key":"secret-key","base_url":%q}`, provider.URL+"/v1")},
			{"GET", metadataPath, ""}, {"PUT", metadataPath, `{"enabled":true}`},
			{"PUT", "/api/v1/mcp/servers/one", `{"name":"disabled"}`},
			{"PATCH", "/api/v1/connectors/owned", `{"refresh_freq":7}`},
		} {
			p, _ := request(test.method, test.path, "owner", test.body)
			assertCode(p, false)
		}
		if snapshot() != before || calls.Load() != count {
			t.Fatal("inactive tenant changed state or called provider")
		}
		if db.Model(&entity.Tenant{}).Where("id = ?", "owner").Update("status", "1").Error != nil {
			t.Fatal("restore tenant")
		}
	})

	t.Run("credential mutations suppress SQL value logging", func(t *testing.T) {
		var trace bytes.Buffer
		previous := dao.DB
		dao.DB = db.Session(&gorm.Session{Logger: logger.New(log.New(&trace, "", 0), logger.Config{LogLevel: logger.Info})})
		defer func() { dao.DB = previous }()
		p, _ := request("PUT", "/api/v1/mcp/servers/one", "owner", `{"headers":{"Authorization":"secret-log-mcp"},"variables":{"token":"secret-log-variable"}}`)
		assertCode(p, true)
		p, _ = request("PATCH", "/api/v1/connectors/owned", "owner", `{"config":{"api_key":"secret-log-connector"}}`)
		assertCode(p, true)
		if strings.Contains(trace.String(), "secret-") {
			t.Fatal("credential was logged in SQL")
		}
	})

	t.Run("tokens independent random values", func(t *testing.T) {
		response, err := service.NewSystemService().CreateAPIToken("owner", &service.CreateAPITokenRequest{})
		if err != nil {
			t.Fatal("create token")
		}
		if response.Beta == nil || len(*response.Beta) != 32 || len(response.Token) != len("multirag-")+43 {
			t.Fatal("invalid token shape")
		}
		if *response.Beta == utility.GenerateBetaAPIToken(response.Token) {
			t.Fatal("beta derived from primary token")
		}
		var beta string
		if reader.QueryRow("SELECT beta FROM t_ai_api_tokens WHERE tenant_id='owner' AND token=$1", response.Token).Scan(&beta) != nil || beta != *response.Beta {
			t.Fatal("token SQL readback")
		}
	})
	if binary := os.Getenv("MULTIRAG_AFTER33_CLI"); binary != "" {
		t.Run("built CLI consumer", func(t *testing.T) {
			before := snapshot()
			for _, test := range []struct {
				user, region, key, base string
				success                 bool
			}{
				{"owner", "default", "", provider.URL + "/v1", true},
				{"owner", "missing", "secret-key", provider.URL + "/v1", false},
				{"owner", "default", "secret-fail", provider.URL + "/v1", false},
				{"member", "default", "secret-key", provider.URL + "/v1", false},
				{"revoked", "default", "secret-key", provider.URL + "/v1", false},
			} {
				command := fmt.Sprintf("CHECK PROVIDER 'vllm' REGION '%s' KEY '%s' URL '%s';", test.region, test.key, test.base)
				j.Event("reserve-cli-process", map[string]any{"binary": binary, "credentials": "request-local argv, never logged", "region": test.region, "user": test.user})
				cmd := exec.CommandContext(context.Background(), binary, "-h", strings.TrimPrefix(api.URL, "http://"), "-t", tokens[test.user], command)
				count := calls.Load()
				output, err := cmd.CombinedOutput()
				if (err == nil) != test.success || strings.Contains(string(output), "secret-") {
					t.Fatal("CLI consumer result or redaction")
				}
				if test.success && !strings.Contains(string(output), "SUCCESS") {
					t.Fatal("CLI success output")
				}
				if (test.region == "missing" || test.user != "owner") && calls.Load() != count {
					t.Fatal("CLI denial made provider call")
				}
				if snapshot() != before {
					t.Fatal("CLI probe wrote SQL")
				}
			}
		})
	}

	j.Event("sql-independent-readback", map[string]any{"verified": true, "endpoint": u.Host, "claims": "JWT/routes, SQL transactions and controlled provider HTTP only"})
}
