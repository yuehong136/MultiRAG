package handler_test

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	gormlog "gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/utility"
)

type countingGraphEngine struct {
	engine.DocEngine
	calls atomic.Int64
}

func (e *countingGraphEngine) Search(ctx context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	e.calls.Add(1)
	return e.DocEngine.Search(ctx, req)
}
func (e *countingGraphEngine) ChunkStoreExists(ctx context.Context, base, dataset string) (bool, error) {
	e.calls.Add(1)
	return e.DocEngine.ChunkStoreExists(ctx, base, dataset)
}
func (e *countingGraphEngine) DeleteChunks(ctx context.Context, condition map[string]interface{}, base, dataset string) (int64, error) {
	e.calls.Add(1)
	return e.DocEngine.DeleteChunks(ctx, condition, base, dataset)
}

type graphLiveConfig struct {
	Tenant      string
	Database    server.DatabaseConfig
	ES          server.ElasticsearchConfig
	Infinity    server.InfinityConfig
	DatasetIDs  map[string]string
	ResourceDir string
}

func TestDatasetGraphHTTPAndSQL(t *testing.T) {
	path := os.Getenv("MULTIRAG_ENGINE_LIVE_CONFIG")
	if path == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned graph config required")
		}
		t.Skip("requires owned SQL and document-engine scratch resources")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var cfg graphLiveConfig
	if err = json.Unmarshal(raw, &cfg); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(cfg.Tenant, "multirag_afterd7_b_") || cfg.Database.Database != cfg.Tenant {
		t.Fatal("unowned SQL fixture")
	}
	t.Chdir(filepath.Join("..", ".."))
	if err = common.Init("error"); err != nil {
		t.Fatal(err)
	}
	kind := server.EngineType(os.Getenv("MULTIRAG_ENGINE_LIVE_KIND"))
	if kind != server.EngineInfinity && kind != server.EngineElasticsearch {
		t.Fatal("explicit engine kind required")
	}
	secret := strings.Repeat("owned-graph-", 4)
	if err = server.InitVariables(statusSigningStore{secret: secret}); err != nil {
		t.Fatal(err)
	}
	local.InitAdminStatus(0, "owned graph acceptance")
	dsn, err := dao.PostgresDSN(cfg.Database)
	if err != nil {
		t.Fatal(err)
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: gormlog.Default.LogMode(gormlog.Silent)})
	if err != nil {
		t.Fatal("owned SQL connection failed")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	old := dao.DB
	dao.DB = db
	defer func() { dao.DB = old }()
	entities := []interface{}{&entity.User{}, &entity.UserTenant{}, &entity.Tenant{}, &entity.Knowledgebase{}, &entity.Document{}, &entity.Connector{}, &entity.Connector2Kb{}, &entity.APIToken{}, &entity.UserCanvas{}}
	if err = db.AutoMigrate(entities...); err != nil {
		t.Fatal(err)
	}
	for _, model := range entities {
		if err = db.Session(&gorm.Session{AllowGlobalUpdate: true}).Unscoped().Delete(model).Error; err != nil {
			t.Fatal(err)
		}
	}
	active, inactive := "1", "0"
	owner := cfg.Tenant
	foreignTenant := cfg.Tenant + "_f"
	jwt := map[string]string{}
	for _, user := range []string{owner, "b-member", "b-outsider", "b-inactive", "b-foreign"} {
		access := strings.Repeat("x", 40)
		email := user + "@graph.invalid"
		if err = db.Create(&entity.User{ID: user, Email: email, Nickname: user, AccessToken: &access, Status: &active, IsActive: true, IsAuthenticated: true}).Error; err != nil {
			t.Fatal(err)
		}
		token, err := utility.SignUserJWT(email, server.GetSecretKey())
		if err != nil {
			t.Fatal(err)
		}
		jwt[user] = token
	}
	for i, member := range []struct{ user, tenant, role, status string }{{owner, cfg.Tenant, "owner", active}, {"b-member", cfg.Tenant, "normal", active}, {"b-inactive", cfg.Tenant, "normal", inactive}, {"b-foreign", foreignTenant, "owner", active}} {
		status := member.status
		if err = db.Create(&entity.UserTenant{ID: fmt.Sprint(i), UserID: member.user, TenantID: member.tenant, Role: member.role, InvitedBy: owner, Status: &status}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, tenant := range []string{cfg.Tenant, foreignTenant} {
		name := "owned graph"
		if err = db.Create(&entity.Tenant{ID: tenant, Name: &name, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for name, id := range cfg.DatasetIDs {
		tenant, permission, status, creator := cfg.Tenant, "me", active, owner
		if name == "team" || name == "empty" {
			permission = "team"
		}
		if name == "disabled" {
			status = inactive
		}
		if name == "foreign" {
			tenant = foreignTenant
			creator = "b-foreign"
		}
		if err = db.Create(&entity.Knowledgebase{ID: id, TenantID: tenant, Name: name, Permission: permission, CreatedBy: creator, Status: &status, ParserConfig: entity.JSONMap{}}).Error; err != nil {
			t.Fatal(err)
		}
	}
	apiToken := "multirag-owned-graph-" + string(kind)
	if err = db.Create(&entity.APIToken{TenantID: cfg.Tenant, Token: apiToken}).Error; err != nil {
		t.Fatal(err)
	}
	if err = engine.Init(&server.DocEngineConfig{Type: kind, ES: &cfg.ES, Infinity: &cfg.Infinity}); err != nil {
		t.Fatal(err)
	}
	store := engine.Get()
	defer engine.Close()
	ctx := t.Context()
	base := "multirag_" + cfg.Tenant
	foreignBase := "multirag_" + foreignTenant
	for _, pair := range []struct{ base, kb string }{{base, cfg.DatasetIDs["team"]}, {base, cfg.DatasetIDs["private"]}, {foreignBase, cfg.DatasetIDs["foreign"]}} {
		if err = store.CreateChunkStore(ctx, pair.base, pair.kb, 2, "naive"); err != nil {
			t.Fatal(err)
		}
	}
	graph := map[string]interface{}{"nodes": []interface{}{}, "edges": []interface{}{}}
	for i := range 280 {
		graph["nodes"] = append(graph["nodes"].([]interface{}), map[string]interface{}{"id": fmt.Sprint(i), "pagerank": i})
		if i > 0 {
			graph["edges"] = append(graph["edges"].([]interface{}), map[string]interface{}{"source": fmt.Sprint(i - 1), "target": fmt.Sprint(i), "weight": i})
		}
	}
	graph["edges"] = append(graph["edges"].([]interface{}), map[string]interface{}{"source": "279", "target": "279", "weight": 10000}, map[string]interface{}{"source": "279", "target": "missing", "weight": 10000})
	content, _ := json.Marshal(graph)
	insert := func(index, kb, id, artifact, content string) {
		t.Helper()
		doc := map[string]interface{}{"id": id, "kb_id": kb, "doc_id": "graph-doc", "content_with_weight": content, "knowledge_graph_kwd": artifact, "q_2_vec": []float64{0, 1}, "available_int": 1}
		if failed, err := store.InsertChunks(ctx, []map[string]interface{}{doc}, index, kb); err != nil || len(failed) > 0 {
			t.Fatal(failed, err)
		}
	}
	for _, kind := range []string{"graph", "subgraph", "entity", "relation", "community_report"} {
		insert(base, cfg.DatasetIDs["team"], "artifact-"+kind, kind, string(content))
	}
	insert(base, cfg.DatasetIDs["team"], "ordinary", "", "ordinary")
	insert(base, cfg.DatasetIDs["private"], "neighbor-graph", "graph", string(content))
	insert(base, cfg.DatasetIDs["private"], "neighbor-ordinary", "", "neighbor")
	insert(foreignBase, cfg.DatasetIDs["foreign"], "foreign-graph", "graph", string(content))
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.Use(gin.Recovery())
	counter := &countingGraphEngine{DocEngine: store}
	datasets := handler.NewDatasetsHandler(service.NewDatasetsService(), counter)
	appRouter := router.NewRouter(handler.NewAuthHandler(), nil, nil, nil, datasets, nil, handler.NewKnowledgebaseHandler(service.NewKnowledgebaseService(), service.NewUserService(), nil), nil, nil, nil, nil, nil, nil, nil, nil, nil)
	appRouter.Setup(app)
	httpServer := httptest.NewServer(app)
	defer httpServer.Close()
	request := func(method, dataset, auth, query string) (int, map[string]interface{}) {
		t.Helper()
		req, err := http.NewRequest(method, httpServer.URL+"/api/v1/datasets/"+dataset+"/graph"+query, nil)
		if err != nil {
			t.Fatal(err)
		}
		if auth != "" {
			req.Header.Set("Authorization", "Bearer "+auth)
		}
		response, err := httpServer.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		var body map[string]interface{}
		if err = json.NewDecoder(response.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		return response.StatusCode, body
	}
	assertSuccess := func(method, dataset, auth string) map[string]interface{} {
		t.Helper()
		status, body := request(method, dataset, auth, "")
		if status != 200 || body["code"] != float64(0) {
			t.Fatalf("%s %s: %d %#v", method, dataset, status, body)
		}
		return body
	}
	for _, auth := range []string{jwt[owner], jwt["b-member"], apiToken} {
		body := assertSuccess("GET", cfg.DatasetIDs["team"], auth)
		g := body["data"].(map[string]interface{})["graph"].(map[string]interface{})
		if len(g["nodes"].([]interface{})) != 256 || len(g["edges"].([]interface{})) != 128 {
			t.Fatal("graph was not cropped", g)
		}
	}
	id := cfg.DatasetIDs["team"]
	dashed := fmt.Sprintf("%s-%s-%s-%s-%s", id[:8], id[8:12], id[12:16], id[16:20], id[20:])
	body := assertSuccess("GET", dashed, jwt[owner])
	if len(body["data"].(map[string]interface{})["graph"].(map[string]interface{})["nodes"].([]interface{})) != 256 {
		t.Fatal("canonical dataset ID lost graph scope")
	}
	assertSuccess("GET", cfg.DatasetIDs["private"], jwt[owner])
	empty := assertSuccess("GET", cfg.DatasetIDs["empty"], jwt["b-member"])
	if len(empty["data"].(map[string]interface{})["graph"].(map[string]interface{})) != 0 {
		t.Fatal("absent graph not empty")
	}
	// Rejections go through the registered auth middleware and current SQL membership.
	for _, denied := range []struct{ dataset, auth string }{{cfg.DatasetIDs["team"], ""}, {cfg.DatasetIDs["team"], "invalid"}, {cfg.DatasetIDs["team"], jwt["b-outsider"]}, {cfg.DatasetIDs["team"], jwt["b-inactive"]}, {cfg.DatasetIDs["team"], jwt["b-foreign"]}, {cfg.DatasetIDs["private"], jwt["b-member"]}, {cfg.DatasetIDs["foreign"], jwt[owner]}, {cfg.DatasetIDs["disabled"], jwt[owner]}} {
		before := counter.calls.Load()
		for _, method := range []string{"GET", "DELETE"} {
			status, body := request(method, denied.dataset, denied.auth, "")
			if status == 200 && body["code"] == float64(0) {
				t.Fatal("denial succeeded", method, denied.dataset)
			}
		}
		if counter.calls.Load() != before {
			t.Fatal("denied request touched document engine")
		}
	}
	if status, body := request("GET", cfg.DatasetIDs["team"], jwt[owner], "?doc_id=graph-doc"); status == 200 && body["code"] == float64(0) {
		t.Fatal("unsupported document subgraph silently ignored")
	}
	// Independent SQL connection proves auth/membership/KB rows did not change.
	independent, err := pool.Conn(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	var count int
	if err = independent.QueryRowContext(ctx, "SELECT count(*) FROM t_ai_knowledgebases").Scan(&count); err != nil || count != 5 {
		t.Fatal(count, err)
	}
	independent.Close()
	result, err := store.Search(ctx, &types.SearchRequest{IndexNames: []string{base}, KbIDs: []string{cfg.DatasetIDs["team"]}, SelectFields: []string{"id", "kb_id", "knowledge_graph_kwd"}, Limit: 20})
	if err != nil || len(result.Chunks) != 6 {
		t.Fatal("denial changed graph rows", result, err)
	}
	// Optional fixed-Web consumer runs while valid graph content is available.
	if consumerDir := os.Getenv("MULTIRAG_GRAPH_CONSUMER_DIR"); consumerDir != "" {
		ready := map[string]interface{}{"BaseURL": httpServer.URL, "Token": jwt[owner], "DatasetID": cfg.DatasetIDs["team"], "Kind": kind}
		data, _ := json.Marshal(ready)
		readyPath := filepath.Join(consumerDir, string(kind)+"-ready.private.json")
		if err = os.WriteFile(readyPath, data, 0600); err != nil {
			t.Fatal(err)
		}
		deadline := time.Now().Add(40 * time.Second)
		done := filepath.Join(consumerDir, string(kind)+"-consumer.done")
		for {
			if _, err = os.Stat(done); err == nil {
				break
			}
			if time.Now().After(deadline) {
				t.Fatal("fixed Web consumer did not finish")
			}
			time.Sleep(100 * time.Millisecond)
		}
	}
	// Corruption is a failure, distinct from an absent graph.
	for _, bad := range []string{`{"broken"`, `[]`, `null`} {
		insert(base, cfg.DatasetIDs["team"], "artifact-graph", "graph", bad)
		if _, body := request("GET", cfg.DatasetIDs["team"], jwt[owner], ""); body["code"] == float64(0) {
			t.Fatal("corrupt/non-object graph became success", bad)
		}
	}

	insert(base, cfg.DatasetIDs["team"], "artifact-graph", "graph", string(content))
	assertSuccess("DELETE", cfg.DatasetIDs["team"], jwt["b-member"])
	result, err = store.Search(ctx, &types.SearchRequest{IndexNames: []string{base}, KbIDs: []string{cfg.DatasetIDs["team"]}, SelectFields: []string{"id", "kb_id", "knowledge_graph_kwd"}, Limit: 20})
	if err != nil || len(result.Chunks) != 1 || result.Chunks[0]["id"] != "ordinary" {
		t.Fatal("graph delete changed ordinary chunks", result, err)
	}
	for _, target := range []struct{ base, kb string }{{base, cfg.DatasetIDs["private"]}, {foreignBase, cfg.DatasetIDs["foreign"]}} {
		result, err := store.Search(ctx, &types.SearchRequest{IndexNames: []string{target.base}, KbIDs: []string{target.kb}, SelectFields: []string{"id", "kb_id", "knowledge_graph_kwd"}, Limit: 20})
		if err != nil || len(result.Chunks) == 0 {
			t.Fatal("graph delete changed neighbor/foreign dataset", result, err)
		}
	}

	// Exercise actual query/delete failures without changing borrowed services.
	if kind == server.EngineElasticsearch {
		toggle := func(action string) {
			t.Helper()
			req, err := http.NewRequest("POST", cfg.ES.Hosts+"/"+base+"/_"+action, nil)
			if err != nil {
				t.Fatal(err)
			}
			req.SetBasicAuth(cfg.ES.Username, cfg.ES.Password)
			res, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			res.Body.Close()
			if res.StatusCode != 200 {
				t.Fatalf("owned index %s failed: %d", action, res.StatusCode)
			}
		}
		toggle("close")
		defer toggle("open")
	} else {
		if err = store.Close(); err != nil {
			t.Fatal(err)
		}
	}
	for _, method := range []string{"GET", "DELETE"} {
		if _, body := request(method, cfg.DatasetIDs["team"], jwt[owner], ""); body["code"] == float64(0) {
			t.Fatal("unavailable engine became successful graph", method)
		}
	}
	t.Log("real router JWT/API-token authentication, SQL memberships, scoped graph read/delete and preserved neighbors passed")
}
