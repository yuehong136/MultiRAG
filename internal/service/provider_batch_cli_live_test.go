package service_test

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/cli"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/utility"
	"multirag/tests/support"
)

// TestProviderBatchCLIHTTPAndSQL runs the actual CLI and JWT routes against an
// exclusive PostgreSQL database. The vLLM wire service is a controlled fixture.
func TestProviderBatchCLIHTTPAndSQL(t *testing.T) {
	config := os.Getenv("MULTIRAG_AFTERAAB_B_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned B PostgreSQL configuration required")
		}
		t.Skip("requires exclusive B PostgreSQL configuration")
	}
	journal := os.Getenv("MULTIRAG_PROVIDER_TEST_JOURNAL")
	if journal == "" || os.Getenv("MULTIRAG_RUN_ID") == "" {
		t.Fatal("registered run and resource journal required")
	}
	record := func(value map[string]any) {
		t.Helper()
		value["case"], value["parent_run_id"] = t.Name(), os.Getenv("MULTIRAG_RUN_ID")
		value["inputs_reference"] = os.Getenv("MULTIRAG_RUN_JOURNAL") + "#reserve-run.inputs"
		file, err := os.OpenFile(journal, os.O_APPEND|os.O_WRONLY, 0600)
		if err != nil {
			t.Fatal(err)
		}
		defer file.Close()
		if err := json.NewEncoder(file).Encode(value); err != nil {
			t.Fatal(err)
		}
		if err := file.Sync(); err != nil {
			t.Fatal(err)
		}
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
		t.Fatal("private configuration load failed")
	}
	cfg := server.GetConfig().Database
	if !strings.HasPrefix(cfg.Database, "multirag_afteraab_b_") || cfg.Host != "127.0.0.1" || cfg.Port <= 0 || cfg.Schema != "usr_ai" || cfg.Driver != "postgres" && cfg.Driver != "postgresql" {
		t.Fatal("configuration must identify the exclusive B TCP PostgreSQL database")
	}
	record(map[string]any{"action": "intent", "kind": "sql-schema", "host": cfg.Host, "port": cfg.Port, "database": cfg.Database, "schema": cfg.Schema, "scope": "dao.InitDB migrations in exclusive scratch database"})
	{
		dsn, err := dao.PostgresDSN(cfg)
		if err != nil {
			t.Fatal(err)
		}
		bootstrap, err := sql.Open("pgx", dsn)
		if err != nil {
			t.Fatal("owned schema connection failed")
		}
		if _, err := bootstrap.Exec("CREATE SCHEMA IF NOT EXISTS usr_ai"); err != nil {
			bootstrap.Close()
			t.Fatal("exclusive schema creation failed", err)
		}
		if err := bootstrap.Close(); err != nil {
			t.Fatal(err)
		}
	}
	if err := dao.InitDB(); err != nil {
		t.Fatal("owned schema initialization failed", err)
	}
	pool, err := dao.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	dsn, err := dao.PostgresDSN(cfg)
	if err != nil {
		t.Fatal(err)
	}
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("independent database connection failed")
	}
	t.Cleanup(func() { readback.Close() })
	var actualDatabase, actualHost string
	var actualPort int
	if err := readback.QueryRow("SELECT current_database(), host(inet_server_addr()), inet_server_port()").Scan(&actualDatabase, &actualHost, &actualPort); err != nil || actualDatabase != cfg.Database || actualHost != cfg.Host || actualPort != cfg.Port {
		t.Fatal("independent TCP endpoint readback mismatch", actualDatabase, actualHost, actualPort, err)
	}
	var schemaCount int
	if err := readback.QueryRow("SELECT count(*) FROM information_schema.schemata WHERE schema_name='usr_ai'").Scan(&schemaCount); err != nil || schemaCount != 1 {
		t.Fatal("independent schema readback", schemaCount, err)
	}
	tables := []string{"tenant_model", "tenant_model_instance", "tenant_model_provider", "t_ai_tenant_llms", "t_ai_tenants", "t_ai_user_tenants", "t_ai_users"}
	for _, table := range tables {
		var count int
		if err := readback.QueryRow("SELECT count(*) FROM " + table).Scan(&count); err != nil || count != 0 {
			t.Fatal("scratch table is not empty", table, count, err)
		}
	}
	t.Cleanup(func() {
		for _, table := range tables {
			if _, err := readback.Exec("DELETE FROM " + table); err != nil {
				t.Error("owned SQL cleanup", table, err)
			}
			var count int
			err := readback.QueryRow("SELECT count(*) FROM " + table).Scan(&count)
			record(map[string]any{"action": "cleanup-readback", "kind": "sql-table", "database": cfg.Database, "schema": cfg.Schema, "table": table, "count": count, "absent": err == nil && count == 0})
			if err != nil || count != 0 {
				t.Error("independent cleanup readback", table, count, err)
			}
		}
	})
	if err := server.InitVariables(nil); err != nil {
		t.Fatal(err)
	}
	local.InitAdminStatus(0, "owned B batch CLI acceptance")
	active := "1"
	tokens := make(map[string]string)
	for _, id := range []string{"afteraab-b-owner", "afteraab-b-other"} {
		tenantID, email := id+"-tenant", id+"@owned.invalid"
		record(map[string]any{"action": "intent", "kind": "sql-identity", "database": cfg.Database, "user": id, "tenant": tenantID})
		if err := dao.DB.Create(&entity.User{ID: id, Email: email, Nickname: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := dao.DB.Create(&entity.Tenant{ID: tenantID, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := dao.DB.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: tenantID, Role: "owner", InvitedBy: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		token, err := utility.SignUserJWT(email, server.GetSecretKey())
		if err != nil {
			t.Fatal(err)
		}
		tokens[id] = token
	}
	const numericModelID int64 = 9007199254740993
	record(map[string]any{"action": "intent", "kind": "sql-legacy-model", "database": cfg.Database, "id": strconv.FormatInt(numericModelID, 10), "tenant": "afteraab-b-owner-tenant"})
	if _, err := readback.Exec("INSERT INTO t_ai_tenant_llms (id,tenant_id,llm_factory,mdl_type,llm_name,max_tokens,status) VALUES ($1,$2,$3,$4,$5,$6,$7)", numericModelID, "afteraab-b-owner-tenant", "vllm", "embedding", "legacy-b-embedding", 0, "1"); err != nil {
		t.Fatal(err)
	}
	if _, err := readback.Exec("UPDATE t_ai_tenants SET tenant_embd_id=$1 WHERE id=$2", numericModelID, "afteraab-b-owner-tenant"); err != nil {
		t.Fatal(err)
	}
	assertLegacyID := func() {
		t.Helper()
		var modelID, selectedID int64
		if err := readback.QueryRow("SELECT m.id,t.tenant_embd_id FROM t_ai_tenant_llms m JOIN t_ai_tenants t ON t.id=m.tenant_id WHERE m.id=$1", numericModelID).Scan(&modelID, &selectedID); err != nil || modelID != numericModelID || selectedID != numericModelID {
			t.Fatal("legacy numeric model reference changed", modelID, selectedID, err)
		}
	}
	var vendorCalls atomic.Int32
	vendor := support.RegisteredHTTPServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		vendorCalls.Add(1)
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if r.Method != "POST" || r.URL.Path != "/v1/embeddings" || body["model"] != "b-embedding" || !reflect.DeepEqual(body["input"], []any{"a", "b"}) {
			t.Error("unexpected controlled embedding request", r.Method, r.URL.Path, body)
		}
		if dimension, present := body["dimensions"]; present && dimension != float64(2) {
			t.Error("invalid provider dimension", dimension)
		}
		fmt.Fprint(w, `{"data":[{"index":1,"embedding":[0,1]},{"index":0,"embedding":[1,0]}]}`)
	}))
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.Use(gin.Recovery())
	var modelRequests atomic.Int32
	var dimensionMu sync.Mutex
	var embeddingDimensions []any
	app.Use(func(c *gin.Context) {
		if strings.HasSuffix(c.Request.URL.Path, "/models") && (c.Request.Method == "POST" || c.Request.Method == "DELETE") {
			modelRequests.Add(1)
		}
		if c.Request.URL.Path == "/api/v1/embeddings" {
			raw, err := io.ReadAll(c.Request.Body)
			if err != nil {
				t.Error(err)
			}
			c.Request.Body = io.NopCloser(bytes.NewReader(raw))
			var body map[string]any
			if err := json.Unmarshal(raw, &body); err != nil {
				t.Error(err)
			}
			dimensionMu.Lock()
			embeddingDimensions = append(embeddingDimensions, body["dimension"])
			dimensionMu.Unlock()
		}
		c.Next()
	})
	svc := service.NewModelProviderService()
	routes := router.NewRouter(handler.NewAuthHandler(), nil, nil, nil, nil, handler.NewSystemHandler(service.NewSystemService()), nil, nil, nil, nil, nil, nil, nil, nil, handler.NewProviderHandler(nil, svc), nil)
	routes.Setup(app)
	api := support.RegisteredHTTPServer(t, app)
	apiURL, err := url.Parse(api.URL)
	if err != nil {
		t.Fatal(err)
	}
	clientFor := func(identity string) *cli.MultiRAGClient {
		client := cli.NewMultiRAGClient("user")
		client.HTTPClient.Host = apiURL.Hostname()
		client.HTTPClient.Port, _ = strconv.Atoi(apiURL.Port())
		client.HTTPClient.LoginToken = tokens[identity]
		return client
	}
	execute := func(identity, input string, success bool) cli.ResponseIf {
		t.Helper()
		command, err := cli.NewParser(input).Parse(false)
		if err != nil {
			t.Fatal("CLI grammar", input, err)
		}
		response, err := clientFor(identity).ExecuteUserCommand(command)
		if success && (err != nil || response == nil) || !success && (err == nil || response != nil) {
			t.Fatal("CLI result", input, response, err)
		}
		return response
	}
	execute("afteraab-b-owner", "ADD PROVIDER 'vllm';", true)
	execute("afteraab-b-owner", "CREATE PROVIDER 'vllm' INSTANCE '0' KEY '' URL '"+vendor.URL+"/v1';", true)
	snapshot := func() string {
		t.Helper()
		var result strings.Builder
		for _, table := range tables {
			var digest string
			if err := readback.QueryRow("SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY id)::text,'')) FROM " + table + " t").Scan(&digest); err != nil {
				t.Fatal(err)
			}
			result.WriteString(digest)
		}
		return result.String()
	}
	mutate := func(identity, input string, success bool) {
		t.Helper()
		beforeRequests, beforeSQL := modelRequests.Load(), snapshot()
		execute(identity, input, success)
		if modelRequests.Load()-beforeRequests != 1 {
			t.Fatal("model batch was not one HTTP request", input, modelRequests.Load()-beforeRequests)
		}
		if !success && snapshot() != beforeSQL {
			t.Fatal("rejected model batch changed SQL state", input)
		}
		assertLegacyID()
	}
	mutate("afteraab-b-owner", "ADD MODEL 'a-chat b-embedding' TO PROVIDER 'vllm' INSTANCE '0' WITH CHAT VISION THINK TOKENS 0, EMBEDDING;", true)
	rows, err := readback.Query("SELECT model_name, model_type, extra, instance_id FROM tenant_model ORDER BY model_name")
	if err != nil {
		t.Fatal(err)
	}
	count := 0
	for rows.Next() {
		var name, capability, extra, instanceID string
		if err := rows.Scan(&name, &capability, &extra, &instanceID); err != nil {
			t.Fatal(err)
		}
		var declaration struct {
			Tokens   *int     `json:"max_tokens"`
			Types    []string `json:"model_types"`
			Thinking *bool    `json:"thinking"`
		}
		if err := json.Unmarshal([]byte(extra), &declaration); err != nil || declaration.Tokens == nil || *declaration.Tokens != 0 || instanceID == "" {
			t.Fatal("invalid persisted model declaration", name, extra, err)
		}
		if name != "a-chat" && name != "b-embedding" || name == "a-chat" && (capability != "chat" || !reflect.DeepEqual(declaration.Types, []string{"chat", "image2text"}) || declaration.Thinking == nil || !*declaration.Thinking) || name == "b-embedding" && (capability != "embedding" || !reflect.DeepEqual(declaration.Types, []string{"embedding"}) || declaration.Thinking != nil) {
			t.Fatal("capabilities were not persisted", name, capability, extra)
		}
		count++
	}
	if err := rows.Close(); err != nil {
		t.Fatal(err)
	}
	if err := rows.Err(); err != nil || count != 2 {
		t.Fatal("independent batch row count", count, err)
	}
	for _, suffix := range []string{"", " DIMENSION 0", " DIMENSION 2"} {
		response := execute("afteraab-b-owner", "EMBED TEXT 'a' 'b' WITH 'b-embedding@0@vllm'"+suffix+";", true)
		embeddings, ok := response.(*cli.EmbeddingsResponse)
		if !ok || len(embeddings.Data) != 2 || embeddings.Data[0].Index != 0 || !reflect.DeepEqual(embeddings.Data[0].Embedding, []float64{1, 0}) {
			t.Fatal("actual provider result was not validated and sorted", response)
		}
	}
	dimensionMu.Lock()
	dimensions := append([]any(nil), embeddingDimensions...)
	dimensionMu.Unlock()
	if !reflect.DeepEqual(dimensions, []any{nil, float64(0), float64(2)}) || vendorCalls.Load() != 3 {
		t.Fatal("omitted/zero/specified dimension wire contract", dimensions, vendorCalls.Load())
	}
	mutate("afteraab-b-owner", "ADD MODEL 'new-first b-embedding' TO PROVIDER 'vllm' INSTANCE '0' WITH CHAT, EMBEDDING;", false)
	mutate("afteraab-b-other", "ADD MODEL 'foreign-one foreign-two' TO PROVIDER 'vllm' INSTANCE '0' WITH CHAT, EMBEDDING;", false)
	mutate("afteraab-b-other", "REMOVE MODEL 'a-chat b-embedding' FROM 'vllm' '0';", false)
	beforeVendor := vendorCalls.Load()
	execute("afteraab-b-other", "EMBED TEXT 'a' 'b' WITH 'b-embedding@0@vllm';", false)
	if vendorCalls.Load() != beforeVendor {
		t.Fatal("foreign inference reached provider")
	}
	mutate("afteraab-b-owner", "DROP MODEL 'a-chat z-missing' FROM 'vllm' '0';", false)
	mutate("afteraab-b-owner", "REMOVE MODEL 'a-chat b-embedding' FROM 'vllm' '0';", true)
	if err := readback.QueryRow("SELECT count(*) FROM tenant_model").Scan(&count); err != nil || count != 0 {
		t.Fatal("independent atomic deletion readback", count, err)
	}
	assertLegacyID()
	record(map[string]any{"action": "assertions-passed", "kind": "cli-http-sql", "database": cfg.Database, "model_requests": modelRequests.Load(), "provider_calls": vendorCalls.Load(), "numeric_legacy_id": strconv.FormatInt(numericModelID, 10), "remaining_custom_models": count})
}
