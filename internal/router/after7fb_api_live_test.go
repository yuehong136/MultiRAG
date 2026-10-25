package router

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/testsupport"
	"multirag/internal/utility"
)

type after7fbEngine struct {
	engine.DocEngine
	fail  atomic.Bool
	calls atomic.Int64
}

func (e *after7fbEngine) DeleteChunks(_ context.Context, condition map[string]interface{}, base, kb string) (int64, error) {
	e.calls.Add(1)
	if base != "multirag_owner" || kb != "kb" {
		return 0, fmt.Errorf("wrong index scope")
	}
	ids, ok := condition["doc_id"].([]string)
	if !ok || len(ids) != 1 || ids[0] != "doc" {
		return 0, fmt.Errorf("wrong document scope")
	}
	if e.fail.Load() {
		return 0, errors.New("private-index-error")
	}
	return 1, nil
}

func TestAfter7fbAPIHTTPAndSQL(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_AFTER7FB_DSN")
	if dsn == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned SQL required")
		}
		t.Skip("requires owned SQL")
	}
	fixture := testsupport.NewCase(t)
	private := os.Getenv("MULTIRAG_AFTER7FB_PRIVATE")
	address := os.Getenv("MULTIRAG_AFTER7FB_ADDRESS")
	if private == "" || address == "" {
		t.Fatal("private config and exact listener required")
	}

	parsed, err := url.Parse(dsn)
	expectedDB := os.Getenv("MULTIRAG_AFTER7FB_DB")
	if err != nil || (parsed.Scheme != "postgres" && parsed.Scheme != "postgresql") || strings.TrimPrefix(parsed.Path, "/") != expectedDB || !strings.HasPrefix(expectedDB, "multirag_after7fb_g_") {
		t.Fatal("explicit owned scratch database required")
	}
	resolvedPrivate, err := filepath.EvalSymlinks(private)
	if err != nil {
		t.Fatal("owned private config directory required")
	}
	tmp := os.Getenv("TMPDIR")
	if tmp == "" {
		t.Fatal("registered temporary root required")
	}
	resolvedTMP, err := filepath.EvalSymlinks(tmp)
	if err != nil {
		t.Fatal("registered temporary root required")
	}
	relative, err := filepath.Rel(resolvedTMP, resolvedPrivate)
	if err != nil || relative == "." || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) || filepath.IsAbs(relative) {
		t.Fatal("private config must be inside registered temporary root")
	}
	host, port, err := net.SplitHostPort(address)
	if err != nil || host != "127.0.0.1" {
		t.Fatal("exact loopback listener required")
	}
	number, err := strconv.Atoi(port)
	if err != nil || number < 1 || number > 65535 {
		t.Fatal("exact loopback port required")
	}
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	setup, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal(err)
	}
	pool, _ := setup.DB()
	defer pool.Close()
	previous := dao.DB
	dao.DB = setup
	defer func() { dao.DB = previous }()
	tables := []any{&entity.User{}, &entity.UserTenant{}, &entity.APIToken{}, &entity.Tenant{}, &entity.TenantLLM{}, &entity.Connector{}, &entity.Connector2Kb{}, &entity.SyncLogs{}, &entity.Knowledgebase{}, &entity.Document{}, &entity.File{}, &entity.File2Document{}, &entity.Task{}, &entity.MCPServer{}}
	names := []string{"t_ai_users", "t_ai_user_tenants", "t_ai_api_tokens", "t_ai_tenants", "t_ai_tenant_llms", "t_ai_connectors", "t_ai_connector2kb", "t_ai_sync_logs", "t_ai_knowledgebases", "t_ai_documents", "t_ai_files", "t_ai_file2documents", "t_ai_tasks", "t_ai_mcp_server"}
	fixture.Event("reserve-fixtures", map[string]any{"tables": names, "scope": "entire owned scratch database", "users": []string{"owner", "member", "admin", "disabled", "foreign"}, "connectors": []string{"connector", "foreign"}, "datasets": []string{"kb", "unbound", "foreign"}})
	if err = setup.AutoMigrate(tables...); err != nil {
		t.Fatal(err)
	}
	if err = setup.Callback().Create().Before("gorm:create").Register("after7fb-reserve-row", func(tx *gorm.DB) {
		fixture.Event("reserve-row-before-SQL", map[string]any{"table": tx.Statement.Table, "identity": tx.Statement.ReflectValue.FieldByName("ID").Interface()})
	}); err != nil {
		t.Fatal(err)
	}
	users := service.NewUserService()
	active, inactive := "1", "0"
	hash, err := users.HashPassword("old-password")
	if err != nil {
		t.Fatal(err)
	}
	must := func(value any) {
		t.Helper()
		if err := setup.Create(value).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, id := range []string{"owner", "member", "admin", "disabled", "foreign"} {
		must(&entity.User{ID: id, Email: id + "@example.invalid", Nickname: id, Status: &active, Password: &hash, IsActive: true, IsAuthenticated: true})
		must(&entity.Tenant{ID: id})
	}
	for _, r := range []struct{ id, role, status string }{{"member", "normal", active}, {"admin", "admin", active}, {"disabled", "normal", inactive}} {
		must(&entity.UserTenant{ID: r.id, UserID: r.id, TenantID: "owner", Role: r.role, Status: &r.status})
	}
	for _, id := range []string{"connector", "foreign"} {
		tenant := "owner"
		if id == "foreign" {
			tenant = "foreign"
		}
		must(&entity.Connector{ID: id, TenantID: tenant, Name: id, Source: "rest-api", InputType: "poll", Status: "5", Config: entity.JSONMap{"token": "private-secret"}})
	}
	for _, id := range []string{"kb", "unbound", "foreign"} {
		tenant := "owner"
		if id == "foreign" {
			tenant = "foreign"
		}
		must(&entity.Knowledgebase{ID: id, TenantID: tenant, CreatedBy: tenant, Name: id, DocNum: 2, TokenNum: 7, ChunkNum: 3, Status: &active})
	}
	must(&entity.Connector2Kb{ID: "binding", ConnectorID: "connector", KbID: "kb"})
	must(&entity.SyncLogs{ID: "scheduled", ConnectorID: "connector", KbID: "kb", Status: "5", ErrorMsg: "private-log-secret"})
	must(&entity.SyncLogs{ID: "foreign-log", ConnectorID: "foreign", KbID: "foreign", Status: "5", ErrorMsg: ""})
	must(&entity.Document{ID: "doc", ParserConfig: entity.JSONMap{}, KbID: "kb", SourceType: "rest-api/connector", CreatedBy: "owner", TokenNum: 7, ChunkNum: 3})

	must(&entity.Document{ID: "guard", KbID: "kb", ParserConfig: entity.JSONMap{}, SourceType: "manual", CreatedBy: "owner"})
	for _, id := range []string{"owned-file", "shared-file", "foreign-file"} {
		tenant := "owner"
		if id == "foreign-file" {
			tenant = "foreign"
		}
		must(&entity.File{ID: id, TenantID: tenant, CreatedBy: tenant, SourceType: "knowledgebase"})
		doc := "doc"
		file := id
		must(&entity.File2Document{ID: id, FileID: &file, DocumentID: &doc})
	}
	guard, shared := "guard", "shared-file"
	must(&entity.File2Document{ID: "guard-map", FileID: &shared, DocumentID: &guard})
	must(&entity.Task{ID: "task", DocID: "doc"})
	for i, id := range []string{"owner", "foreign"} {
		name, kind := "chat-model", "chat"
		must(&entity.TenantLLM{ID: int64(i + 1), TenantID: id, LLMFactory: "OpenAI", LLMName: &name, ModelType: &kind, Status: "1"})
	}
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer readback.Close()
	snapshot := func(t *testing.T) string {
		t.Helper()
		parts := []string{}
		for _, name := range names {
			var raw string
			if err := readback.QueryRow("SELECT COALESCE(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text)::text,'[]') FROM " + name + " x").Scan(&raw); err != nil {
				t.Fatal(err)
			}
			parts = append(parts, raw)
		}
		hash := sha256.Sum256([]byte(strings.Join(parts, "\n")))
		return hex.EncodeToString(hash[:])
	}
	t.Chdir(private)
	fixture.Event("reserve-private-key", map[string]any{"path": filepath.Join(private, "configs/private.pem")})
	if err = os.MkdirAll("configs", 0700); err != nil {
		t.Fatal(err)
	}
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile("configs/private.pem", pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)}), 0600); err != nil {
		t.Fatal(err)
	}
	keyInput, err := os.ReadFile("configs/private.pem")
	if err != nil {
		t.Fatal(err)
	}
	keyHash := sha256.Sum256(keyInput)
	fixture.Event("private-key-input-before-password-requests", map[string]any{"path": filepath.Join(private, "configs/private.pem"), "sha256": hex.EncodeToString(keyHash[:])})
	encrypt := func(value string) string {
		raw, err := rsa.EncryptPKCS1v15(rand.Reader, &key.PublicKey, []byte(base64.StdEncoding.EncodeToString([]byte(value))))
		if err != nil {
			t.Fatal(err)
		}
		return base64.StdEncoding.EncodeToString(raw)
	}
	if err = server.FromConfigFile(filepath.Join(private, "config.yaml")); err != nil {
		t.Fatal(err)
	}
	if err = server.InitVariables(nil); err != nil {
		t.Fatal(err)
	}
	oldSecret := server.GetSecretKey()
	server.SetSecretKey("owned-after7fb-key")
	defer server.SetSecretKey(oldSecret)
	oldAdmin := local.GetAdminStatus()
	local.SetAdminStatus(0, "after7fb")
	defer local.SetAdminStatus(oldAdmin.Status, oldAdmin.Reason)
	index := &after7fbEngine{}
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	(&Router{authHandler: handler.NewAuthHandler(), userHandler: handler.NewUserHandler(users), tenantHandler: handler.NewTenantHandler(service.NewTenantService(), users), connectorHandler: handler.NewConnectorHandler(service.NewConnectorService(index), users), systemHandler: handler.NewSystemHandler(service.NewSystemService())}).Setup(app)
	fixture.Event("reserve-listener", map[string]any{"address": address})
	listener, err := net.Listen("tcp", address)
	if err != nil {
		t.Fatal(err)
	}
	live := &httptest.Server{Listener: listener, Config: &http.Server{Handler: app}}
	live.Start()
	defer func() {
		live.Close()
		conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
		if conn != nil {
			conn.Close()
		}
		fixture.Event("cleanup-listener-readback", map[string]any{"address": address, "absent": err != nil})
		if err == nil {
			t.Error("listener remains")
		}
	}()
	transport := http.DefaultTransport.(*http.Transport).Clone()
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 8 * time.Second}
	trap := &connectorOutboundTrap{}
	oldTransport := http.DefaultTransport
	http.DefaultTransport = trap
	defer func() { http.DefaultTransport = oldTransport }()
	request := func(t *testing.T, actor, method, path, body string) (int, map[string]any) {
		t.Helper()
		req, err := http.NewRequest(method, live.URL+path, strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		if actor != "" {
			token, err := utility.SignUserJWT(actor+"@example.invalid", server.GetSecretKey())
			if err != nil {
				t.Fatal(err)
			}
			req.Header.Set("Authorization", "Bearer "+token)
		}
		response, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		raw, err := io.ReadAll(response.Body)
		response.Body.Close()
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(raw), "private-") || strings.Contains(string(raw), "old-password") {
			t.Fatalf("secret response: %s", raw)
		}
		var decoded map[string]any
		if err = json.Unmarshal(raw, &decoded); err != nil {
			t.Fatal(err, string(raw))
		}
		return response.StatusCode, decoded
	}
	run := func(name, actor, method, path, body string, success bool, check func(map[string]any)) {
		t.Run(name, func(t *testing.T) {
			j := testsupport.NewCase(t)
			j.Event("reserve-request", map[string]any{"actor": actor, "method": method, "path": path, "database": os.Getenv("MULTIRAG_AFTER7FB_DB"), "scope": names})
			before := snapshot(t)
			calls := index.calls.Load()
			status, result := request(t, actor, method, path, body)
			after := snapshot(t)
			j.Event("independent-SQL-readback", map[string]any{"before": before, "after": after, "http_status": status, "business_code": result["code"], "index_calls": index.calls.Load() - calls, "source_calls": trap.calls.Load()})
			if (result["code"] == float64(0)) != success {
				t.Fatal(result)
			}
			if !success && (before != after || index.calls.Load() != calls) {
				t.Fatal("denied/invalid request had effects")
			}
			if trap.calls.Load() != 0 {
				t.Fatal("configuration endpoint contacted source")
			}
			if check != nil {
				check(result)
			}
		})
	}
	connectorPath := "/api/v1/connectors/"
	run("unauth-create", "", "POST", connectorPath, `{"name":"x","source":"rest-api","config":{}}`, false, nil)
	for i, body := range []string{`{}`, `{"name":"x","source":"rest-api","config":null}`, `{"name":"x","source":"rest-api","config":{},"refresh_freq":-1}`, `{"name":"x","source":"rest-api","config":{},"tenant_id":"foreign"}`, `{"name":"x","source":"rest-api","config":{},"refresh_freq":1.5}`} {
		run(fmt.Sprint("invalid-create-", i), "owner", "POST", connectorPath, body, false, nil)
	}
	run("create-defaults", "owner", "POST", connectorPath, `{"name":"saved","source":"rest-api","config":{"token":"private-secret"},"refresh_freq":0,"prune_freq":0}`, true, func(result map[string]any) {
		data := result["data"].(map[string]any)
		if data["refresh_freq"] != float64(0) || data["timeout_secs"] != float64(1740) || data["status"] != "0" || data["config"] != nil {
			t.Fatal(data)
		}
	})

	run("list-owned-and-joined", "member", "GET", connectorPath, "", true, func(result map[string]any) {
		rows := result["data"].([]any)
		if len(rows) != 2 {
			t.Fatal(rows)
		}
	})
	run("list-disabled-membership", "disabled", "GET", connectorPath, "", true, func(result map[string]any) {
		rows := result["data"].([]any)
		if len(rows) != 0 {
			t.Fatal(rows)
		}
	})

	for _, field := range []string{"name", "source"} {
		for _, length := range []int{128, 129} {
			payload := map[string]any{"name": "unicode-boundary", "source": "rest-api", "config": map[string]any{}}
			payload[field] = strings.Repeat("连", length)
			raw, _ := json.Marshal(payload)
			var check func(map[string]any)
			if length == 128 {
				check = func(result map[string]any) {
					id := result["data"].(map[string]any)["id"].(string)
					var chars, bytes int
					query := "SELECT char_length(" + field + "),octet_length(" + field + ") FROM t_ai_connectors WHERE id=$1"
					if err := readback.QueryRow(query, id).Scan(&chars, &bytes); err != nil || chars != 128 || bytes != 384 {
						t.Fatal(chars, bytes, err)
					}
				}
			}
			run(fmt.Sprintf("create-CJK-%s-%d", field, length), "owner", "POST", connectorPath, string(raw), length == 128, check)
		}
	}
	run("create-invalid-UTF8", "owner", "POST", connectorPath, "{\"name\":\"\xff\",\"source\":\"rest-api\",\"config\":{}}", false, nil)
	settings := "/api/v1/users/me"
	run("partial-settings", "owner", "PATCH", settings, `{"nickname":"new"}`, true, func(_ map[string]any) {
		var name, email string
		var lang sql.NullString
		if err := readback.QueryRow("SELECT nickname,email,language FROM t_ai_users WHERE id='owner'").Scan(&name, &email, &lang); err != nil || name != "new" || email != "owner@example.invalid" || lang.Valid {
			t.Fatal(name, email, lang, err)
		}
	})
	run("password-without-old", "owner", "PATCH", settings, `{"nickname":"bad","new_password":"invalid"}`, false, nil)
	run("password-wrong", "owner", "PATCH", settings, `{"nickname":"bad","password":"`+encrypt("wrong-password")+`","new_password":"`+encrypt("new-password")+`"}`, false, nil)
	run("password-malformed", "owner", "PATCH", settings, `{"password":"invalid"}`, false, nil)
	run("password-success", "owner", "PATCH", settings, `{"password":"`+encrypt("old-password")+`","new_password":"`+encrypt("new-password")+`"}`, true, func(_ map[string]any) {
		var stored string
		if err := readback.QueryRow("SELECT password FROM t_ai_users WHERE id='owner'").Scan(&stored); err != nil || !users.VerifyPassword(stored, "new-password") {
			t.Fatal("password readback", err)
		}
	})
	for i, body := range []string{`{"email":"other@example.invalid"}`, `{"nickname":null}`, `{"nickname":"a","nickname":"b"}`} {
		run(fmt.Sprint("settings-invalid-", i), "owner", "PATCH", settings, body, false, nil)
	}
	models := settings + "/models"
	run("models-foreign-tenant", "owner", "PATCH", models, `{"tenant_id":"foreign","llm_id":"chat-model@OpenAI"}`, false, nil)
	run("models-normal-member", "member", "PATCH", models, `{"tenant_id":"owner","llm_id":"chat-model@OpenAI"}`, false, nil)
	run("models-id-injection", "owner", "PATCH", models, `{"tenant_llm_id":2}`, false, nil)
	run("models-foreign-name", "owner", "PATCH", models, `{"llm_id":"missing@OpenAI","tts_id":""}`, false, nil)
	run("models-wrong-capability", "owner", "PATCH", models, `{"embd_id":"chat-model@OpenAI"}`, false, nil)
	run("models-resolve", "owner", "PATCH", models, `{"llm_id":"chat-model@OpenAI"}`, true, func(_ map[string]any) {
		var id int64
		if err := readback.QueryRow("SELECT tenant_llm_id FROM t_ai_tenants WHERE id='owner'").Scan(&id); err != nil || id != 1 {
			t.Fatal(id, err)
		}
	})
	run("models-clear", "owner", "PATCH", models, `{"llm_id":""}`, true, func(_ map[string]any) {
		var id sql.NullInt64
		var name string
		if err := readback.QueryRow("SELECT tenant_llm_id,llm_id FROM t_ai_tenants WHERE id='owner'").Scan(&id, &name); err != nil || id.Valid || name != "" {
			t.Fatal(id, name, err)
		}
	})
	mcp := "/api/v1/mcp/servers"
	run("mcp-create", "owner", "POST", mcp, `{"name":"saved","url":"https://never-connect.invalid/mcp","server_type":"sse","headers":{"Authorization":"private-secret"},"variables":{"token":"private-secret","tools":{"x":1}}}`, true, func(result map[string]any) {
		data := result["data"].(map[string]any)
		if len(data["headers"].(map[string]any)) != 0 {
			t.Fatal(data)
		}
	})
	for i, body := range []string{`{"name":"saved","url":"https://never-connect.invalid","server_type":"sse"}`, `{"name":"bad","url":"file:///etc/passwd","server_type":"sse"}`, `{"name":"bad","url":"http://example.invalid","server_type":"sse","headers":[]}`, `{"name":"bad","url":"http://example.invalid","server_type":"stdio"}`} {
		run(fmt.Sprint("mcp-invalid-", i), "owner", "POST", mcp, body, false, nil)
	}
	t.Run("mcp-concurrent-duplicate", func(t *testing.T) {
		j := testsupport.NewCase(t)
		j.Event("reserve-concurrent-requests", map[string]any{"count": 4, "name": "concurrent"})
		var wg sync.WaitGroup
		var successes atomic.Int64
		for i := 0; i < 4; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				_, result := request(t, "owner", "POST", mcp, `{"name":"concurrent","url":"http://never-connect.invalid","server_type":"streamable-http"}`)
				if result["code"] == float64(0) {
					successes.Add(1)
				}
			}()
		}
		wg.Wait()
		var count int
		if err := readback.QueryRow("SELECT count(*) FROM t_ai_mcp_server WHERE name='concurrent' AND tenant_id='owner'").Scan(&count); err != nil || count != 1 || successes.Load() != 1 {
			t.Fatal(count, successes.Load(), err)
		}
		j.Event("independent-SQL-readback", map[string]any{"count": count, "successes": successes.Load()})
	})
	for _, actor := range []string{"foreign", "disabled"} {
		run("logs-denied-"+actor, actor, "GET", connectorPath+"connector/logs", "", false, nil)
		run("delete-denied-"+actor, actor, "DELETE", connectorPath+"connector", "", false, nil)
		run("rebuild-denied-"+actor, actor, "POST", connectorPath+"connector/rebuild", `{"kb_id":"kb"}`, false, nil)
	}
	for i, query := range []string{"page_size=101", "page_size=0", "page=-1", "page=1&page=2", "page_size=", "page_size=1.5", "page_size=%20", "page=%09"} {
		run(fmt.Sprint("logs-pagination-", i), "owner", "GET", connectorPath+"connector/logs?"+query, "", false, nil)
	}
	run("logs-private-hidden-member", "member", "GET", connectorPath+"connector/logs?page_size=100", "", true, func(result map[string]any) {
		data := result["data"].(map[string]any)
		if data["total"] != float64(0) || len(data["logs"].([]any)) != 0 {
			t.Fatal(data)
		}
	})
	run("logs-owner", "owner", "GET", connectorPath+"connector/logs?page_size=100", "", true, func(result map[string]any) {
		data := result["data"].(map[string]any)
		if data["total"] != float64(1) {
			t.Fatal(data)
		}
	})
	run("rebuild-private-dataset-member", "member", "POST", connectorPath+"connector/rebuild", `{"kb_id":"kb"}`, false, nil)
	for _, kb := range []string{"foreign", "unbound"} {
		run("rebuild-binding-"+kb, "owner", "POST", connectorPath+"connector/rebuild", `{"kb_id":"`+kb+`"}`, false, nil)
	}
	t.Run("rebuild-index-failure", func(t *testing.T) {
		j := testsupport.NewCase(t)
		j.Event("reserve-request", map[string]any{"scope": "doc/kb/multirag_owner", "index": "controlled engine failure"})
		index.fail.Store(true)
		defer index.fail.Store(false)
		before := snapshot(t)
		_, result := request(t, "owner", "POST", connectorPath+"connector/rebuild", `{"kb_id":"kb"}`)
		after := snapshot(t)
		if result["code"] == float64(0) || before != after {
			t.Fatal(result, "SQL changed on index failure")
		}
		j.Event("independent-SQL-readback", map[string]any{"before": before, "after": after})
	})
	run("rebuild-success", "owner", "POST", connectorPath+"connector/rebuild", `{"kb_id":"kb"}`, true, func(_ map[string]any) {
		var docs, logs int
		readback.QueryRow("SELECT count(*) FROM t_ai_documents WHERE id='doc'").Scan(&docs)
		readback.QueryRow("SELECT count(*) FROM t_ai_sync_logs WHERE connector_id='connector' AND status='5' AND from_beginning='1'").Scan(&logs)

		var files, guardRows, docCount, tokenCount, chunkCount int
		if err := readback.QueryRow("SELECT count(*) FROM t_ai_files WHERE id IN ('shared-file','foreign-file')").Scan(&files); err != nil || files != 2 {
			t.Fatal(files, err)
		}
		if err := readback.QueryRow("SELECT count(*) FROM t_ai_files WHERE id='owned-file'").Scan(&files); err != nil || files != 0 {
			t.Fatal(files, err)
		}
		if err := readback.QueryRow("SELECT count(*) FROM t_ai_file2documents WHERE id='guard-map'").Scan(&guardRows); err != nil || guardRows != 1 {
			t.Fatal(guardRows, err)
		}
		if err := readback.QueryRow("SELECT doc_num,token_num,chunk_num FROM t_ai_knowledgebases WHERE id='kb'").Scan(&docCount, &tokenCount, &chunkCount); err != nil || docCount != 1 || tokenCount != 0 || chunkCount != 0 {
			t.Fatal(docCount, tokenCount, chunkCount, err)
		}
		if docs != 0 || logs != 1 {
			t.Fatal(docs, logs)
		}
	})

	t.Run("delete-SQL-failure-rolls-back", func(t *testing.T) {
		j := testsupport.NewCase(t)
		j.Event("reserve-fault", map[string]any{"callback": "after7fb-delete-fault", "table": "t_ai_connector2kb"})
		if err := setup.Callback().Delete().Before("gorm:delete").Register("after7fb-delete-fault", func(tx *gorm.DB) {
			if tx.Statement.Table == "t_ai_connector2kb" {
				tx.AddError(errors.New("private-SQL-failure"))
			}
		}); err != nil {
			t.Fatal(err)
		}
		defer setup.Callback().Delete().Remove("after7fb-delete-fault")
		before := snapshot(t)
		_, result := request(t, "owner", "DELETE", connectorPath+"connector", "")
		after := snapshot(t)
		if result["code"] == float64(0) || before != after {
			t.Fatal(result, "partial SQL mutation")
		}
		j.Event("independent-SQL-readback", map[string]any{"before": before, "after": after})
	})
	run("delete-success", "owner", "DELETE", connectorPath+"connector", "", true, func(_ map[string]any) {
		var count int
		readback.QueryRow("SELECT count(*) FROM t_ai_connector2kb WHERE connector_id='connector'").Scan(&count)
		if count != 0 {
			t.Fatal("binding remains")
		}
		readback.QueryRow("SELECT count(*) FROM t_ai_sync_logs WHERE connector_id='connector' AND status='5'").Scan(&count)
		if count != 0 {
			t.Fatal("scheduled logs remain")
		}
	})
	t.Run("health-partial", func(t *testing.T) {
		j := testsupport.NewCase(t)
		j.Event("reserve-request", map[string]any{"path": "/api/v1/system/healthz", "dependencies": "real SQL; redis/index/storage uninitialized"})
		status, result := request(t, "", "GET", "/api/v1/system/healthz", "")
		if status != 500 || result["db"] != "ok" || result["status"] != "nok" || result["redis"] != "nok" {
			t.Fatal(status, result)
		}
		j.Event("health-readback", result)
	})
	if err := setup.Callback().Create().Remove("after7fb-reserve-row"); err != nil {
		t.Fatal(err)
	}
}
