package handler_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	gormlog "gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine"
	esengine "multirag/internal/engine/elasticsearch"
	infengine "multirag/internal/engine/infinity"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/tokenizer"
	"multirag/internal/utility"
)

type datasetChunkEngine struct {
	engine.DocEngine
	reads  atomic.Int64
	writes atomic.Int64
	fail   atomic.Bool
}

func (e *datasetChunkEngine) GetChunk(ctx context.Context, base, chunk string, datasets []string) (any, error) {
	e.reads.Add(1)
	if e.fail.Load() {
		return nil, errors.New("controlled storage failure")
	}
	return e.DocEngine.GetChunk(ctx, base, chunk, datasets)
}
func (e *datasetChunkEngine) Search(ctx context.Context, request *types.SearchRequest) (*types.SearchResult, error) {
	e.reads.Add(1)
	if e.fail.Load() {
		return nil, errors.New("controlled storage failure")
	}
	return e.DocEngine.Search(ctx, request)
}
func (e *datasetChunkEngine) UpdateChunks(ctx context.Context, condition, value map[string]any, base, dataset string) error {
	e.writes.Add(1)
	if e.fail.Load() {
		return errors.New("controlled storage failure")
	}
	return e.DocEngine.UpdateChunks(ctx, condition, value, base, dataset)
}

func (e *datasetChunkEngine) DeleteChunks(ctx context.Context, condition map[string]any, base, dataset string) (int64, error) {
	e.writes.Add(1)
	if e.fail.Load() {
		return 0, errors.New("controlled storage failure")
	}
	return e.DocEngine.DeleteChunks(ctx, condition, base, dataset)
}

// This opt-in binds registered routes, SQL authorization, both stores and a freshly built CLI.
func TestDatasetChunkMetadataCLIAndStores(t *testing.T) {
	path := os.Getenv("MULTIRAG_GO_DATASET_CHUNK_CONFIG")
	if path == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned dataset chunk config required")
		}
		t.Skip("requires owned PostgreSQL and document-store resources")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var cfg struct {
		graphLiveConfig
		HTTPPort int
	}
	if err := json.Unmarshal(raw, &cfg); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(cfg.Tenant, "multirag_after2836_e_") || cfg.Database.Database != cfg.Tenant || cfg.Infinity.DBName != cfg.Tenant {
		t.Fatal("explicit owned SQL/Infinity namespace required")
	}
	if cfg.HTTPPort < 1024 || cfg.HTTPPort > 65535 {
		t.Fatal("registered scratch HTTP port is required")
	}
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.InitVariables(statusSigningStore{secret: strings.Repeat("owned-chunk-", 4)}); err != nil {
		t.Fatal(err)
	}
	local.InitAdminStatus(0, "owned chunk acceptance")
	dsn, err := dao.PostgresDSN(cfg.Database)
	if err != nil {
		t.Fatal(err)
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: gormlog.Default.LogMode(gormlog.Silent)})
	if err != nil {
		t.Fatal("owned SQL connection failed")
	}
	pool, _ := db.DB()
	defer pool.Close()
	old := dao.DB
	dao.DB = db
	defer func() { dao.DB = old }()
	entities := []any{&entity.User{}, &entity.UserTenant{}, &entity.Tenant{}, &entity.Knowledgebase{}, &entity.Document{}, &entity.Connector{}, &entity.Connector2Kb{}, &entity.APIToken{}, &entity.UserCanvas{}}
	if err := db.AutoMigrate(entities...); err != nil {
		t.Fatal(err)
	}
	for _, model := range entities {
		if err := db.Session(&gorm.Session{AllowGlobalUpdate: true}).Unscoped().Delete(model).Error; err != nil {
			t.Fatal(err)
		}
	}
	active, inactive := "1", "0"
	tokens := map[string]string{}
	for _, id := range []string{"owner", "member", "outsider", "inactive", "foreign", "multi"} {
		email, access := id+"@chunk.invalid", strings.Repeat("x", 40)
		if err := db.Create(&entity.User{ID: id, Email: email, Nickname: id, AccessToken: &access, Status: &active, IsActive: true, IsAuthenticated: true}).Error; err != nil {
			t.Fatal(err)
		}
		tokens[id], err = utility.SignUserJWT(email, server.GetSecretKey())
		if err != nil {
			t.Fatal(err)
		}
	}
	for i, m := range []struct{ user, tenant, status string }{{"owner", cfg.Tenant, active}, {"member", cfg.Tenant, active}, {"inactive", cfg.Tenant, inactive}, {"foreign", cfg.Tenant + "_f", active}, {"multi", cfg.Tenant, active}, {"multi", cfg.Tenant + "_f", active}} {
		status := m.status
		if err := db.Create(&entity.UserTenant{ID: fmt.Sprint(i), UserID: m.user, TenantID: m.tenant, Role: "normal", InvitedBy: "owner", Status: &status}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, tenant := range []string{cfg.Tenant, cfg.Tenant + "_f"} {
		name := "owned chunk"
		if err := db.Create(&entity.Tenant{ID: tenant, Name: &name, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for kind, id := range cfg.DatasetIDs {
		tenant, creator, permission, status := cfg.Tenant, "owner", "team", active
		if kind == "private" {
			permission = "me"
		}
		if kind == "disabled" {
			status = inactive
		}
		if kind == "foreign" {
			tenant, creator = cfg.Tenant+"_f", "foreign"
		}
		if err := db.Create(&entity.Knowledgebase{ID: id, TenantID: tenant, Name: "dataset " + kind, CreatedBy: creator, Permission: permission, Status: &status, ParserID: "naive", ParserConfig: entity.JSONMap{}}).Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Create(&entity.Document{ID: "doc-" + kind, KbID: id, CreatedBy: creator, ParserID: "naive", ParserConfig: entity.JSONMap{}, Type: "doc", Suffix: "txt", Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	kind := os.Getenv("MULTIRAG_ENGINE_LIVE_KIND")
	var store engine.DocEngine
	switch kind {
	case "infinity":
		store, err = infengine.NewEngine(&cfg.Infinity)
	case "elasticsearch":
		store, err = esengine.NewEngine(&cfg.ES)
	default:
		t.Fatal("explicit document engine kind required")
	}
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	ctx := t.Context()
	for _, tenant := range []string{cfg.Tenant, cfg.Tenant + "_f"} {
		if exists, err := store.MetadataStoreExists(ctx, tenant); err != nil {
			t.Fatal(err)
		} else if exists {
			if err := store.DropMetadataStore(ctx, tenant); err != nil {
				t.Fatal(err)
			}
		}
		if err := store.CreateMetadataStore(ctx, tenant); err != nil {
			t.Fatal(err)
		}
	}
	for name, id := range cfg.DatasetIDs {
		tenant := cfg.Tenant
		if name == "foreign" {
			tenant += "_f"
		}
		base := "multirag_" + tenant
		if exists, err := store.ChunkStoreExists(ctx, base, id); err != nil {
			t.Fatal(err)
		} else if exists {
			if err := store.DropChunkStore(ctx, base, id); err != nil {
				t.Fatal(err)
			}
		}
		if err := store.CreateChunkStore(ctx, base, id, 2, "naive"); err != nil {
			t.Fatal(err)
		}
		row := map[string]any{"id": "chunk-" + name, "doc_id": "doc-" + name, "kb_id": id, "content_with_weight": "body " + name, "q_2_vec": []float64{0, 1}, "available_int": 0, "tag_kwd": []string{"keep", "remove", "remove2", "web-remove"}}
		if failed, err := store.InsertChunks(ctx, []map[string]any{row}, base, id); err != nil || len(failed) > 0 {
			t.Fatal(failed, err)
		}
		if failed, err := store.InsertMetadata(ctx, []map[string]any{{"id": "doc-" + name, "kb_id": id, "meta_fields": map[string]any{"kind": name, "rank": 0, "enabled": false}}}, tenant); err != nil || len(failed) > 0 {
			t.Fatal(failed, err)
		}
	}
	counter := &datasetChunkEngine{DocEngine: store}
	datasets := handler.NewDatasetsHandler(service.NewDatasetsService(), counter)
	chunks := handler.NewChunkHandler(service.NewChunkService(counter), service.NewUserService())
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.Use(gin.Recovery())
	router.NewRouter(handler.NewAuthHandler(), nil, nil, nil, datasets, nil, handler.NewKnowledgebaseHandler(service.NewKnowledgebaseService(), service.NewUserService(), nil), chunks, nil, nil, nil, nil, nil, nil, nil, nil, nil).Setup(app)
	listener, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", cfg.HTTPPort))
	if err != nil {
		t.Fatal(err)
	}
	httpServer := &httptest.Server{Listener: listener, Config: &http.Server{Handler: app}}
	httpServer.Start()
	defer httpServer.Close()
	request := func(t *testing.T, method, path, user, body string, want int) map[string]any {
		t.Helper()
		req, err := http.NewRequestWithContext(ctx, method, httpServer.URL+path, strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		if user != "" {
			req.Header.Set("Authorization", "Bearer "+tokens[user])
		}
		req.Header.Set("Content-Type", "application/json")
		response, err := httpServer.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		var result map[string]any
		if err := json.NewDecoder(response.Body).Decode(&result); err != nil {
			t.Fatal(err, response.StatusCode)
		}
		if result["code"] != float64(want) {
			t.Fatalf("%s %s user=%s: HTTP %d body=%v want code=%d", method, path, user, response.StatusCode, result, want)
		}
		return result
	}
	a := cfg.DatasetIDs["a"]
	chunkPath := "/api/v1/datasets/" + a + "/documents/doc-a/chunks/chunk-a"
	metadataPath := "/api/v1/datasets/metadata/flattened?dataset_ids=" + a + "," + cfg.DatasetIDs["b"]
	tagPath := "/api/v1/datasets/" + a + "/tags"
	if kind == "infinity" {
		t.Run("bound_parent_merge_across_datasets", func(t *testing.T) {
			for i, dataset := range []string{a, cfg.DatasetIDs["b"]} {
				content, image := "", "filled"
				if i == 0 {
					content, image = "first nonempty", ""
				}
				row := map[string]any{"id": "parent-merge", "doc_id": "doc-a", "kb_id": dataset, "content_with_weight": content, "img_id": image, "q_2_vec": []float64{0, 1}, "available_int": 0}
				if failed, err := store.InsertChunks(ctx, []map[string]any{row}, "multirag_"+cfg.Tenant, dataset); err != nil || len(failed) > 0 {
					t.Fatal(failed, err)
				}
			}
			parent, err := store.GetChunk(ctx, "multirag_"+cfg.Tenant, "parent-merge", []string{a, cfg.DatasetIDs["b"]})
			if err != nil {
				t.Fatal(err)
			}
			row, ok := parent.(map[string]any)
			if !ok || row["content_with_weight"] != "first nonempty" || row["img_id"] != "filled" {
				t.Fatal("explicit multi-dataset parent merge changed", parent)
			}
			for _, dataset := range []string{a, cfg.DatasetIDs["b"]} {
				if count, err := store.DeleteChunks(ctx, map[string]any{"id": "parent-merge"}, "multirag_"+cfg.Tenant, dataset); err != nil || count != 1 {
					t.Fatal(count, err)
				}
			}
		})
	}
	t.Run("authorized_identity_and_zero", func(t *testing.T) {
		data := request(t, "GET", chunkPath+"?chunk_id=wrong", "member", "", 0)["data"].(map[string]any)
		if data["id"] != "chunk-a" || data["doc_id"] != "doc-a" || data["kb_id"] != a || data["available_int"] != float64(0) || data["q_2_vec"] != nil {
			t.Fatal(data)
		}
		meta := request(t, "GET", metadataPath, "member", "", 0)["data"].(map[string]any)
		if !reflect.DeepEqual(meta["rank"].(map[string]any)["0"], []any{"doc-a", "doc-b"}) || meta["enabled"].(map[string]any)["false"] == nil {
			t.Fatal(meta)
		}
		request(t, "GET", "/api/v1/datasets/metadata/flattened?dataset_ids="+a+","+cfg.DatasetIDs["foreign"], "multi", "", 0)
	})
	t.Run("denials_have_zero_store_calls", func(t *testing.T) {
		for _, user := range []string{"", "outsider", "inactive"} {
			want := 403
			if user == "" {
				want = 401
			}
			reads, writes := counter.reads.Load(), counter.writes.Load()
			request(t, "GET", chunkPath, user, "", want)
			request(t, "GET", metadataPath, user, "", want)
			request(t, "DELETE", tagPath, user, `{"tags":["remove"]}`, want)
			if reads != counter.reads.Load() || writes != counter.writes.Load() {
				t.Fatal("denied request reached storage")
			}
		}
		before := counter.reads.Load()
		request(t, "GET", "/api/v1/datasets/metadata/flattened?dataset_ids="+a+","+cfg.DatasetIDs["foreign"], "owner", "", 403)
		request(t, "GET", "/api/v1/datasets/"+cfg.DatasetIDs["private"]+"/documents/doc-private/chunks/chunk-private", "member", "", 403)
		request(t, "GET", "/api/v1/datasets/"+cfg.DatasetIDs["disabled"]+"/documents/doc-disabled/chunks/chunk-disabled", "owner", "", 403)
		request(t, "GET", "/api/v1/datasets/"+a+"/documents/doc-b/chunks/chunk-a", "owner", "", 404)
		if before != counter.reads.Load() {
			t.Fatal("invalid binding reached storage")
		}
		request(t, "GET", "/api/v1/datasets/"+a+"/documents/doc-a/chunks/chunk-b", "owner", "", 404)
	})
	t.Run("malformed_requests_and_storage_errors", func(t *testing.T) {
		before := counter.writes.Load()
		for _, body := range []string{`{}`, `{"tags":null}`, `{"tags":[]}`, `{"tags":[""]}`, `{"tags":[" "]}`, `{"tags":["remove","remove"]}`, `{"tags":[1]}`, `{"tags":["remove"],"all":true}`, `{"tags":["remove"],"tags":["keep"]}`, `{"tags":["remove"]} {}`} {
			request(t, "DELETE", tagPath, "owner", body, 102)
		}
		if before != counter.writes.Load() {
			t.Fatal("invalid tags mutated storage")
		}
		for _, query := range []string{"", "?dataset_ids=", "?dataset_ids=" + a + ",", "?dataset_ids=" + a + "," + a, "?dataset_ids=" + a + "&dataset_ids=" + a} {
			request(t, "GET", "/api/v1/datasets/metadata/flattened"+query, "owner", "", 102)
		}
		counter.fail.Store(true)
		request(t, "GET", chunkPath, "owner", "", 500)
		request(t, "GET", metadataPath, "owner", "", 500)
		request(t, "DELETE", tagPath, "owner", `{"tags":["remove"]}`, 500)
		counter.fail.Store(false)
	})
	t.Run("fixed_web_consumer", func(t *testing.T) {
		web := os.Getenv("WEB_DATASET_CHECKOUT")
		if web == "" {
			t.Fatal("fixed Web consumer checkout required")
		}
		script, err := filepath.Abs("tests/support/dataset_chunk_web_consumer.mts")
		if err != nil {
			t.Fatal(err)
		}
		ids, _ := json.Marshal(cfg.DatasetIDs)
		command := exec.CommandContext(ctx, filepath.Join(web, "node_modules/.bin/tsx"), "--tsconfig", filepath.Join(web, "tsconfig.app.json"), script)
		command.Dir = web
		command.Env = append(os.Environ(), "DATASET_CHUNK_WEB_BASE="+httpServer.URL, "DATASET_CHUNK_WEB_TOKEN="+tokens["owner"], "DATASET_CHUNK_WEB_IDS="+string(ids))
		output, err := command.CombinedOutput()
		if err != nil {
			t.Fatalf("fixed Web consumer failed: %v %s", err, output)
		}
		t.Logf("Web consumer: %s", output)
	})
	t.Run("native_cli_and_exact_deletion", func(t *testing.T) {
		binary := os.Getenv("MULTIRAG_GO_DATASET_CHUNK_CLI")
		if binary == "" {
			t.Fatal("freshly linked CLI binary required")
		}
		for _, input := range []string{"GET CHUNK 'chunk-a' OF DATASET '" + a + "' DOCUMENT 'doc-a';", "LIST METADATA OF DATASET '" + a + "' '" + cfg.DatasetIDs["b"] + "';", "REMOVE TAGS 'remove', 'remove2' FROM DATASET '" + a + "';"} {
			command := exec.CommandContext(ctx, binary, "--host", strings.TrimPrefix(httpServer.URL, "http://"), "--token", tokens["owner"], "-o", "json", input)
			output, err := command.CombinedOutput()
			if err != nil {
				t.Fatalf("CLI failed: %s %v %s", input, err, output)
			}
			if strings.Contains(string(output), "ERROR") {
				t.Fatal("CLI hid a business failure", string(output))
			}
			t.Logf("native CLI %s output=%s", strings.Fields(input)[0], output)
		}
		for name, dataset := range cfg.DatasetIDs {
			tenant := cfg.Tenant
			if name == "foreign" {
				tenant += "_f"
			}
			rows, err := store.Search(ctx, &types.SearchRequest{IndexNames: []string{"multirag_" + tenant}, KbIDs: []string{dataset}, Limit: 10, IncludeUnavailable: true, SelectFields: []string{"id", "tag_kwd", "available_int"}})
			if err != nil || len(rows.Chunks) != 1 {
				t.Fatal(name, rows, err)
			}
			tags := fmt.Sprint(rows.Chunks[0]["tag_kwd"])
			if !strings.Contains(tags, "keep") || strings.Contains(tags, "remove") != (name != "a") {
				t.Fatal("tag deletion crossed dataset scope or failed", name, tags)
			}
		}
		var docs int64
		if err := db.Model(&entity.Document{}).Count(&docs).Error; err != nil || docs != int64(len(cfg.DatasetIDs)) {
			t.Fatal("tag mutation changed SQL document state", docs, err)
		}
	})
	t.Run("native_update_and_space_separated_chunk_removal", func(t *testing.T) {
		if err := tokenizer.Init(&tokenizer.PoolConfig{DictPath: cfg.ResourceDir, MinSize: 1, MaxSize: 1}); err != nil {
			t.Fatal(err)
		}
		defer tokenizer.Close()
		if err := db.Create(&entity.Document{ID: "doc-other", KbID: a, CreatedBy: "owner", ParserID: "naive", ParserConfig: entity.JSONMap{}, Type: "doc", Suffix: "txt", Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		extras := []map[string]any{}
		for _, id := range []string{"delete-one", "delete-two", "chunk-other"} {
			doc := "doc-a"
			if id == "chunk-other" {
				doc = "doc-other"
			}
			extras = append(extras, map[string]any{"id": id, "doc_id": doc, "kb_id": a, "content_with_weight": "extra", "q_2_vec": []float64{0, 1}, "tag_kwd": []string{"keep"}, "available_int": 0})
		}
		if failed, err := store.InsertChunks(ctx, extras, "multirag_"+cfg.Tenant, a); err != nil || len(failed) > 0 {
			t.Fatal(failed, err)
		}
		before := counter.writes.Load()
		request(t, "POST", "/api/v1/chunk/update", "owner", fmt.Sprintf(`{"dataset_id":%q,"document_id":"doc-a","chunk_id":"chunk-other","content":"forbidden"}`, a), 404)
		request(t, "POST", "/api/v1/chunk/rm", "member", `{"doc_id":"doc-private","delete_all":true}`, 403)
		if before != counter.writes.Load() {
			t.Fatal("invalid mutation reached store")
		}
		binary := os.Getenv("MULTIRAG_GO_DATASET_CHUNK_CLI")
		for _, input := range []string{fmt.Sprintf(`UPDATE CHUNK 'chunk-a' OF DATASET '%s' DOCUMENT 'doc-a' SET '{"content":"updated a","available":false}';`, a), `REMOVE CHUNKS 'delete-one' 'delete-two' FROM DOCUMENT 'doc-a';`} {
			output, err := exec.CommandContext(ctx, binary, "--host", strings.TrimPrefix(httpServer.URL, "http://"), "--token", tokens["owner"], input).CombinedOutput()
			if err != nil || strings.Contains(string(output), "ERROR") {
				t.Fatalf("native mutation failed: %s %v %s", input, err, output)
			}
			t.Logf("native mutation %s: %s", strings.Fields(input)[0], output)
		}
		rows, err := store.Search(ctx, &types.SearchRequest{IndexNames: []string{"multirag_" + cfg.Tenant}, KbIDs: []string{a}, IncludeUnavailable: true, Limit: 10, SelectFields: []string{"id", "doc_id", "content_with_weight", "available_int"}})
		if err != nil || len(rows.Chunks) != 2 {
			t.Fatal("exact chunk deletion failed", rows, err)
		}
		for _, row := range rows.Chunks {
			switch row["id"] {
			case "chunk-a":
				if row["content_with_weight"] != "updated a" {
					t.Fatal(row)
				}
			case "chunk-other":
				if row["doc_id"] != "doc-other" || row["content_with_weight"] != "extra" {
					t.Fatal(row)
				}
			default:
				t.Fatal("unexpected remaining row", row)
			}
		}
	})
	t.Run("old_routes_retired", func(t *testing.T) {
		for _, target := range []struct{ method, path string }{{"GET", "/api/v1/chunk/get?chunk_id=chunk-a"}, {"POST", "/api/v1/kb/" + a + "/rm_tags"}} {
			req := httptest.NewRequest(target.method, target.path, strings.NewReader(`{"tags":["keep"]}`))
			req.Header.Set("Authorization", "Bearer "+tokens["owner"])
			response := httptest.NewRecorder()
			app.ServeHTTP(response, req)
			if response.Code != 404 {
				t.Fatal("obsolete route remains", target.path, response.Code)
			}
		}
	})
}
