package handler_test

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	infinitysdk "github.com/infiniflow/infinity-go-sdk"
	"io"
	"multirag/internal/cli"
	"multirag/internal/engine"
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
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/service"
	"multirag/internal/service/nlp"
	"multirag/internal/tokenizer"
)

// Controlled authenticated identities, real handlers/HTTP and independently read SQL.
func TestDatasetReadHTTPAndSQL(t *testing.T) {
	config := os.Getenv("MULTIRAG_DATASET_READ_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned dataset scratch config required")
		}
		t.Skip("requires owned dataset scratch database")
	}
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal("load owned config failed")
	}
	if server.GetConfig().Database.Database != "multirag_after9b_c" {
		t.Fatal("unowned database")
	}
	if err := dao.InitDB(); err != nil {
		t.Fatal("scratch initialization failed", err)
	}
	pool, _ := dao.DB.DB()
	defer pool.Close()
	dsn, err := dao.PostgresDSN(server.GetConfig().Database)
	if err != nil {
		t.Fatal(err)
	}
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer readback.Close()
	active, inactive := "1", "0"
	for i, member := range []struct{ user, tenant, status string }{{"owner", "after9b_c", active}, {"member", "after9b_c", active}, {"revoked", "after9b_c", inactive}, {"foreign", "elsewhere", active}} {
		if err := dao.DB.Create(&entity.UserTenant{ID: fmt.Sprint(i), UserID: member.user, TenantID: member.tenant, Role: func() string {
			if member.user == "member" {
				return "normal"
			}
			return "owner"
		}(), InvitedBy: "owner", Status: &member.status}).Error; err != nil {
			t.Fatal(err)
		}
	}
	tenantName := "owned scratch"
	if err := dao.DB.Create(&entity.Tenant{ID: "after9b_c", Name: &tenantName, Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	ids := map[string]string{}
	for _, kind := range []string{"private", "shared", "empty", "foreign", "disabled"} {
		id, _ := uuid.NewUUID()
		ids[kind] = strings.ReplaceAll(id.String(), "-", "")
		tenant, permission, status := "after9b_c", "me", active
		if kind == "shared" {
			permission = "team"
		}
		if kind == "foreign" {
			tenant = "elsewhere"
		}
		if kind == "disabled" {
			status = inactive
		}
		if err := dao.DB.Create(&entity.Knowledgebase{ID: ids[kind], TenantID: tenant, Name: kind, EmbdID: "unconfigured", Permission: permission, CreatedBy: "owner", Status: &status}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for i, size := range []int64{12, 34} {
		if err := dao.DB.Create(&entity.Document{ID: fmt.Sprintf("read-doc-%d", i), KbID: ids["shared"], Type: "txt", Suffix: "txt", CreatedBy: "owner", Size: size, Status: &active, MetaFields: &entity.JSONMap{}, ParserConfig: entity.JSONMap{}}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, tenant := range []string{"after9b_c", "elsewhere"} {
		if err := dao.DB.Create(&entity.Connector{ID: tenant, TenantID: tenant, Name: tenant, Source: "s3", InputType: "poll", Config: entity.JSONMap{"secret": "must-never-leak"}, Status: "schedule"}).Error; err != nil {
			t.Fatal(err)
		}
		if err := dao.DB.Create(&entity.Connector2Kb{ID: tenant, ConnectorID: tenant, KbID: ids["shared"], AutoParse: "0"}).Error; err != nil {
			t.Fatal(err)
		}
	}
	snapshot := func() string {
		t.Helper()
		var result string
		if err := readback.QueryRow(`SELECT (SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM t_ai_knowledgebases x) || (SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM t_ai_documents x) || (SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM t_ai_connectors x) || (SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM t_ai_connector2kb x)`).Scan(&result); err != nil {
			t.Fatal(err)
		}
		return result
	}
	tokenizerConfig := &tokenizer.PoolConfig{DictPath: "resource", MinSize: 1, MaxSize: 2}
	if err := tokenizer.Init(tokenizerConfig); err != nil {
		t.Fatal(err)
	}
	defer tokenizer.Close()
	if err := nlp.InitQueryBuilderFromTokenizer(tokenizerConfig.DictPath); err != nil {
		t.Fatal(err)
	}
	uri := os.Getenv("MULTIRAG_DATASET_READ_INFINITY_URI")
	if uri == "" {
		t.Fatal("owned Infinity URI required")
	}
	if err := engine.Init(&server.DocEngineConfig{Type: server.EngineInfinity, Infinity: &server.InfinityConfig{URI: uri, DBName: "multirag_after9b_c"}}); err != nil {
		t.Fatal(err)
	}
	defer engine.Close()
	store := engine.Get()
	host, portText, err := net.SplitHostPort(uri)
	if err != nil {
		t.Fatal(err)
	}
	sdkPort, err := strconv.Atoi(portText)
	if err != nil {
		t.Fatal(err)
	}
	indexReadback, err := infinitysdk.Connect(infinitysdk.NetworkAddress{IP: host, Port: sdkPort})
	if err != nil {
		t.Fatal(err)
	}
	defer indexReadback.Disconnect()
	indexDB, err := indexReadback.GetDatabase("multirag_after9b_c")
	if err != nil {
		t.Fatal(err)
	}

	for _, kind := range []string{"shared", "private"} {
		docID := "read-doc-0"
		if kind == "private" {
			docID = "private-doc"
			if err := dao.DB.Create(&entity.Document{ID: docID, KbID: ids[kind], Type: "txt", Suffix: "txt", CreatedBy: "owner", Status: &active, MetaFields: &entity.JSONMap{}, ParserConfig: entity.JSONMap{}}).Error; err != nil {
				t.Fatal(err)
			}
		}
		t.Log("owned-index-resource", "multirag_after9b_c", "multirag_after9b_c_"+ids[kind])
		if err := store.CreateDataset(context.Background(), "multirag_after9b_c", ids[kind], 2, "naive"); err != nil {
			t.Fatal(err)
		}
		defer func(kind string) {
			tableName := "multirag_after9b_c_" + ids[kind]
			if err := store.DropTable(context.Background(), tableName); err != nil {
				t.Error(err)
			}
			if _, err := indexDB.GetTable(tableName); err == nil {
				t.Error("owned table survived cleanup")
			}
		}(kind)
		failed, err := store.InsertDataset(context.Background(), []map[string]any{{"id": kind, "kb_id": ids[kind], "doc_id": docID, "available_int": 1, "content": "availability " + kind, "docnm": "document", "q_2_vec": []float64{0, 1}}}, "multirag_after9b_c", ids[kind])
		if err != nil || len(failed) > 0 {
			t.Fatal("real index insert failed", failed, err)
		}
	}
	for _, kind := range []string{"shared", "private"} {
		table, err := indexDB.GetTable("multirag_after9b_c_" + ids[kind])
		if err != nil {
			t.Fatal(err)
		}
		raw, err := table.Output([]string{"id", "kb_id"}).ToResult()
		if err != nil {
			t.Fatal(err)
		}
		rows, ok := raw.(*infinitysdk.QueryResult)
		if !ok || len(rows.Data["id"]) != 1 || rows.Data["id"][0] != kind || rows.Data["kb_id"][0] != ids[kind] {
			t.Fatal("independent index readback mismatch", raw)
		}
	}
	var embeddingCalls atomic.Int32
	providerAPI := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		embeddingCalls.Add(1)
		if r.URL.Path != "/embeddings" || r.Header.Get("Authorization") != "Bearer controlled-key" {
			t.Error("wrong provider endpoint or credential")
		}
		io.WriteString(w, `{"data":[{"index":0,"embedding":[0,1]}]}`)
	}))
	defer providerAPI.Close()
	extra, _ := json.Marshal(map[string]string{"region": "", "base_url": providerAPI.URL})
	name, modelType, key := "embedding-test", "embedding", "controlled-key"
	for _, row := range []any{&entity.TenantModelProvider{ID: "retrieval-provider", ProviderName: "ZHIPU-AI", TenantID: "after9b_c"}, &entity.TenantModelInstance{ID: "retrieval-instance", ProviderID: "retrieval-provider", InstanceName: "default", APIKey: key, Status: "active", Extra: string(extra)}, &entity.TenantModel{ID: "retrieval-model", ProviderID: "retrieval-provider", InstanceID: "retrieval-instance", ModelName: name, ModelType: modelType, Status: "active", Extra: `{"model_types":["embedding"],"max_tokens":8192}`}, &entity.TenantLLM{TenantID: "after9b_c", LLMFactory: "ZHIPU-AI", LLMName: &name, ModelType: &modelType, APIKey: &key}} {
		if err := dao.DB.Create(row).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := dao.DB.Model(&entity.Knowledgebase{}).Where("tenant_id = ?", "after9b_c").Update("embd_id", name+"@ZHIPU-AI").Error; err != nil {
		t.Fatal(err)
	}
	before := snapshot()
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.Use(func(c *gin.Context) {
		id := strings.TrimPrefix(c.GetHeader("Authorization"), "Bearer ")
		if id != "" {
			c.Set("user", &entity.User{ID: id})
		}
		c.Next()
	})
	datasets := handler.NewDatasetsHandler(service.NewDatasetsService())
	app.GET("/api/v1/datasets/:dataset_id", datasets.GetDataset)
	app.GET("/api/v1/datasets", datasets.ListDatasets)
	chunks := handler.NewChunkHandler(service.NewChunkService(), nil)
	app.POST("/api/v1/datasets/search", chunks.SearchDatasets)
	api := httptest.NewServer(app)
	defer api.Close()
	call := func(method, path, user, body string) (int, map[string]any, string) {
		t.Helper()
		req, err := http.NewRequest(method, api.URL+path, strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		if user != "" {
			req.Header.Set("Authorization", "Bearer "+user)
		}
		req.Header.Set("Content-Type", "application/json")
		resp, err := api.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		raw, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatal(err)
		}
		var out map[string]any
		if err := json.Unmarshal(raw, &out); err != nil {
			t.Fatal(err)
		}
		return resp.StatusCode, out, string(raw)
	}
	for _, tc := range []struct {
		kind, user string
		allow      bool
	}{{"shared", "owner", true}, {"shared", "member", true}, {"private", "owner", true}, {"private", "member", false}, {"shared", "revoked", false}, {"shared", "foreign", false}, {"foreign", "member", false}, {"disabled", "owner", false}, {"empty", "owner", true}, {"shared", "", false}} {
		status, out, raw := call("GET", "/api/v1/datasets/"+ids[tc.kind], tc.user, "")
		if strings.Contains(raw, "must-never-leak") {
			t.Fatal("connector config leaked")
		}
		if tc.allow {
			if status != 200 || out["code"] != float64(0) {
				t.Fatalf("%+v: %d %s", tc, status, raw)
			}
			data := out["data"].(map[string]any)
			if tc.kind == "shared" {
				if data["size"] != float64(46) || len(data["connectors"].([]any)) != 1 {
					t.Fatal(raw)
				}
				if data["connectors"].([]any)[0].(map[string]any)["id"] != "after9b_c" {
					t.Fatal("foreign connector leaked", raw)
				}
			}
			if tc.kind == "empty" && (data["size"] != float64(0) || len(data["connectors"].([]any)) != 0) {
				t.Fatal(raw)
			}
		} else if out["code"] == float64(0) || out["data"] != nil {
			t.Fatalf("denial leaked: %d %s", status, raw)
		}
	}
	for _, id := range []string{"malformed", strings.ReplaceAll(uuid.NewString(), "-", "")} {
		_, out, _ := call("GET", "/api/v1/datasets/"+id, "owner", "")
		if out["code"] == float64(0) || out["data"] != nil {
			t.Fatal("invalid ID accepted", out)
		}
	}
	for _, tc := range []struct{ user, kind string }{{"member", "shared"}, {"owner", "private"}} {
		body, _ := json.Marshal(map[string]any{"dataset_ids": []string{ids[tc.kind]}, "question": "availability", "similarity_threshold": 0})
		_, out, raw := call("POST", "/api/v1/datasets/search", tc.user, string(body))
		if out["code"] != float64(0) {
			t.Fatalf("real retrieval failed: %s", raw)
		}
		chunks := out["data"].(map[string]any)["chunks"].([]any)
		if len(chunks) != 1 || chunks[0].(map[string]any)["kb_id"] != ids[tc.kind] {
			t.Fatal("retrieval scope lost", raw)
		}
	}
	parsed, _ := url.Parse(api.URL)
	port, _ := strconv.Atoi(parsed.Port())
	consumer := cli.NewMultiRAGClient("user")
	consumer.HTTPClient.Host = parsed.Hostname()
	consumer.HTTPClient.Port = port
	consumer.HTTPClient.LoginToken = "member"
	// SearchOnDatasets resolves names via the real list endpoint; select the shared dataset only.
	cmd := cli.NewCommand("search_on_datasets")
	cmd.Params["datasets"] = "shared"
	cmd.Params["question"] = "availability"
	if _, err := consumer.ExecuteCommand(cmd); err != nil {
		t.Fatal("real CLI retrieval failed", err)
	}
	beforeDenials := embeddingCalls.Load()
	// A mixed scope must fail before any model or document-engine access. No engine is installed.
	for _, selected := range [][]string{{ids["private"]}, {ids["shared"], ids["private"]}, {ids["foreign"]}, {ids["disabled"]}} {
		body, _ := json.Marshal(map[string]any{"dataset_ids": selected, "question": "availability"})
		_, out, raw := call("POST", "/api/v1/datasets/search", "member", string(body))
		if out["code"] == float64(0) || out["data"] != nil || !strings.Contains(raw, "not accessible") {
			t.Fatal("retrieval denial failed", raw)
		}
	}
	if embeddingCalls.Load() != beforeDenials {
		t.Fatal("denial called provider")
	}
	if after := snapshot(); after != before {
		t.Fatal("read/denial mutated SQL state")
	}
	// The aggregate fails closed without returning a partially populated dataset.
	if err := dao.DB.Exec("ALTER TABLE t_ai_documents RENAME TO c_documents_unavailable").Error; err != nil {
		t.Fatal(err)
	}
	_, out, raw := call("GET", "/api/v1/datasets/"+ids["shared"], "member", "")
	if err := dao.DB.Exec("ALTER TABLE c_documents_unavailable RENAME TO t_ai_documents").Error; err != nil {
		t.Fatal(err)
	}
	if out["code"] != float64(common.CodeServerError) || out["data"] != nil || strings.Contains(raw, "shared") {
		t.Fatal("partial failure leaked", raw)
	}
	if snapshot() != before {
		t.Fatal("failure mutated SQL")
	}
	if err := dao.DB.Exec(`CREATE FUNCTION c_reject_provider() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'controlled_provider_creation_failure'; END $$`).Error; err != nil {
		t.Fatal(err)
	}
	if err := dao.DB.Exec(`CREATE TRIGGER c_reject_provider BEFORE INSERT ON tenant_model_provider FOR EACH ROW EXECUTE FUNCTION c_reject_provider()`).Error; err != nil {
		t.Fatal(err)
	}
	code, createErr := service.NewModelProviderService().AddModelProvider("ZHIPU-AI", "owner")
	if err := dao.DB.Exec(`DROP TRIGGER c_reject_provider ON tenant_model_provider`).Error; err != nil {
		t.Fatal(err)
	}
	if err := dao.DB.Exec(`DROP FUNCTION c_reject_provider()`).Error; err != nil {
		t.Fatal(err)
	}
	if code != common.CodeServerError || createErr == nil || !strings.Contains(createErr.Error(), "controlled_provider_creation_failure") {
		t.Fatalf("provider creation cause missing: %v", createErr)
	}
	var providers int
	if err := readback.QueryRow(`SELECT count(*) FROM tenant_model_provider`).Scan(&providers); err != nil || providers != 1 {
		t.Fatal("failed provider creation mutated SQL", err)
	}
	t.Log("real Infinity and CLI retrieval passed; controlled embedding calls", embeddingCalls.Load())
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, _, err := service.NewDatasetsService().GetDataset(ctx, ids["shared"], "member"); err == nil {
		t.Fatal("SQL cancellation ignored")
	}
	t.Log("independent SQL readback: total size 46; private/shared/revoked/foreign/disabled/mixed scopes; zero mutations; aggregate failure closed")
}
