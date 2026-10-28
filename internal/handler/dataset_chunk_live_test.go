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
	"multirag/internal/service/nlp"
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

func (e *datasetChunkEngine) GetChunkForManagement(ctx context.Context, base, chunk string, datasets []string) (any, error) {
	e.reads.Add(1)
	if e.fail.Load() {
		return nil, errors.New("controlled storage failure")
	}
	if reader, ok := e.DocEngine.(engine.ManagementChunkReader); ok {
		return reader.GetChunkForManagement(ctx, base, chunk, datasets)
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

func (e *datasetChunkEngine) DeleteMetadataKeys(ctx context.Context, id, kb string, keys []string, tenant string) error {
	e.writes.Add(1)
	if e.fail.Load() {
		return errors.New("controlled storage failure")
	}
	return e.DocEngine.DeleteMetadataKeys(ctx, id, kb, keys, tenant)
}
func (e *datasetChunkEngine) DeleteMetadata(ctx context.Context, condition map[string]any, tenant string) (int64, error) {
	e.writes.Add(1)
	if e.fail.Load() {
		return 0, errors.New("controlled storage failure")
	}
	return e.DocEngine.DeleteMetadata(ctx, condition, tenant)
}
func (e *datasetChunkEngine) UpdateMetadata(ctx context.Context, id, kb string, fields map[string]any, tenant string) error {
	e.writes.Add(1)
	if e.fail.Load() {
		return errors.New("controlled storage failure")
	}
	return e.DocEngine.UpdateMetadata(ctx, id, kb, fields, tenant)
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
	if (!strings.HasPrefix(cfg.Tenant, "multirag_after2836_e_") && !strings.HasPrefix(cfg.Tenant, "multirag_after4776_g_")) || cfg.Database.Database != cfg.Tenant || cfg.Infinity.DBName != cfg.Tenant {
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
	router.NewRouter(handler.NewAuthHandler(), nil, handler.NewTenantHandler(service.NewTenantService(counter), service.NewUserService()), handler.NewDocumentHandler(service.NewDocumentService(counter)), datasets, nil, handler.NewKnowledgebaseHandler(service.NewKnowledgebaseService(counter), service.NewUserService(), nil), chunks, nil, nil, nil, nil, nil, nil, nil, nil).Setup(app)
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
	if kind == "elasticsearch" {
		t.Run("retrieval_parent_visibility_stays_default", func(t *testing.T) {
			base := "multirag_" + cfg.Tenant
			defer func() {
				if count, err := store.DeleteChunks(ctx, map[string]any{"id": "visibility-parent"}, base, a); err != nil || count != 1 {
					t.Errorf("owned parent cleanup: count=%d error=%v", count, err)
				}
			}()
			parent := map[string]any{"id": "visibility-parent", "doc_id": "doc-a", "kb_id": a, "content_with_weight": "parent marker", "q_2_vec": []float64{0, 1}, "available_int": 0}
			if failed, err := store.InsertChunks(ctx, []map[string]any{parent}, base, a); err != nil || len(failed) > 0 {
				t.Fatal(failed, err)
			}
			child := map[string]any{"id": "visible-child", "chunk_id": "visible-child", "mom_id": "visibility-parent", "doc_id": "doc-a", "kb_id": a, "content_with_weight": "child marker", "content_ltks": "child marker", "available_int": 1, "similarity": 1.0}
			if row, err := store.GetChunk(ctx, base, "visibility-parent", []string{a}); err != nil || row != nil {
				t.Fatal("ordinary GetChunk exposed disabled parent", row, err)
			}
			if got := nlp.RetrievalByChildren([]map[string]any{child}, []string{cfg.Tenant}, store, ctx); len(got) != 0 {
				t.Fatal("disabled ES parent entered retrieval", got)
			}
			data := request(t, "GET", "/api/v1/datasets/"+a+"/documents/doc-a/chunks/visibility-parent", "owner", "", 0)["data"].(map[string]any)
			if data["available_int"] != float64(0) {
				t.Fatal("management read lost disabled parent", data)
			}
			if err := store.UpdateChunks(ctx, map[string]any{"id": "visibility-parent"}, map[string]any{"available_int": 1}, base, a); err != nil {
				t.Fatal(err)
			}
			if got := nlp.RetrievalByChildren([]map[string]any{child}, []string{cfg.Tenant}, store, ctx); len(got) != 1 || got[0]["content_with_weight"] != "parent marker" {
				t.Fatal("enabled parent no longer expands", got)
			}
		})
	}
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
		for _, input := range []string{"GET CHUNK 'chunk-a' OF DOCUMENT 'doc-a' IN DATASET '" + a + "';", "LIST METADATA OF DATASET '" + a + "' '" + cfg.DatasetIDs["b"] + "';", "REMOVE TAGS 'remove', 'remove2' FROM DATASET '" + a + "';"} {
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
		request(t, "DELETE", "/api/v1/datasets/"+cfg.DatasetIDs["private"]+"/documents/doc-private/chunks", "member", `{"delete_all":true}`, 403)
		if before != counter.writes.Load() {
			t.Fatal("invalid mutation reached store")
		}
		binary := os.Getenv("MULTIRAG_GO_DATASET_CHUNK_CLI")
		for _, input := range []string{fmt.Sprintf(`UPDATE CHUNK 'chunk-a' OF DOCUMENT 'doc-a' IN DATASET '%s' SET '{"content":"updated a","available":false}';`, a), fmt.Sprintf(`REMOVE CHUNKS 'delete-one' 'delete-two' FROM DOCUMENT 'doc-a' IN DATASET '%s';`, a)} {
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
	t.Run("canonical_file_import_keeps_sql_namespace", func(t *testing.T) {
		directory := filepath.Join(filepath.Dir(path), "imports")
		if err := os.MkdirAll(directory, 0700); err != nil {
			t.Fatal(err)
		}
		input := map[string]any{"index_name": "multirag_" + cfg.Tenant, "knowledgebase_id": a, "chunks": []map[string]any{{"id": "import-one", "doc_id": "doc-a", "kb_id": a, "content_with_weight": "import marker", "docnm_kwd": "import document", "available_int": 1, "q_2_vec": []float64{0, 1}}}}
		target := filepath.Join(directory, "chunks-"+kind+".json")
		write := func() {
			t.Helper()
			raw, _ := json.Marshal(input)
			if err := os.WriteFile(target, raw, 0600); err != nil {
				t.Fatal(err)
			}
		}
		write()
		binary := os.Getenv("MULTIRAG_GO_DATASET_CHUNK_CLI")
		out, err := exec.CommandContext(ctx, binary, "--host", strings.TrimPrefix(httpServer.URL, "http://"), "--token", tokens["owner"], fmt.Sprintf("INSERT CHUNKS FROM FILE '%s';", target)).CombinedOutput()
		if err != nil {
			t.Fatalf("file import CLI: %v %s", err, out)
		}
		row, err := store.GetChunk(ctx, "multirag_"+cfg.Tenant, "import-one", []string{a})
		if err != nil || row == nil {
			t.Fatal("file import not committed", row, err)
		}
		result, err := nlp.NewRetrievalService(store).Search(ctx, &nlp.RetrievalSearchRequest{TenantIDs: []string{cfg.Tenant}, KbIDs: []string{a}, Question: "", Page: 1, PageSize: 10, Filter: map[string]any{"available_int": 1}})
		if err != nil || !reflect.DeepEqual(result.IDs, []string{"import-one"}) || result.Field["import-one"]["content_with_weight"] != "import marker" || len(result.Aggregation) != 1 || result.Aggregation[0]["key"] != "import document" {
			t.Fatal("retrieval result helpers lost logical identity/fields/count", result, err)
		}
		highlighted := store.GetHighlight(result.Chunks, []string{"import"}, "content_with_weight")
		if !strings.Contains(highlighted["import-one"], "<em>import</em>") {
			t.Fatal("retrieval highlight missing", highlighted)
		}

		request(t, "POST", "/api/v1/tenant/insert_chunks_from_file", "outsider", fmt.Sprintf(`{"file_path":%q}`, target), 403)
		input["index_name"] = "unowned_index_should_never_be_written"
		write()
		request(t, "POST", "/api/v1/tenant/insert_chunks_from_file", "owner", fmt.Sprintf(`{"file_path":%q}`, target), 102)
		input["index_name"] = "multirag_" + cfg.Tenant
		input["chunks"].([]map[string]any)[0]["doc_id"] = "doc-b"
		write()
		request(t, "POST", "/api/v1/tenant/insert_chunks_from_file", "owner", fmt.Sprintf(`{"file_path":%q}`, target), 102)
		row, err = store.GetChunk(ctx, "multirag_"+cfg.Tenant, "import-one", []string{a})
		if err != nil || row.(map[string]any)["doc_id"] != "doc-a" {
			t.Fatal("invalid imports changed original row", row, err)
		}
		if count, err := store.DeleteChunks(ctx, map[string]any{"id": "import-one"}, "multirag_"+cfg.Tenant, a); err != nil || count != 1 {
			t.Fatal(count, err)
		}
	})

	t.Run("metadata_deletion_http_cli_and_independent_readback", func(t *testing.T) {
		docsBefore := []entity.Document{}
		if err := db.Order("id").Find(&docsBefore).Error; err != nil {
			t.Fatal(err)
		}
		readMeta := func(tenant, dataset, doc string) []map[string]any {
			t.Helper()
			result, err := store.Search(ctx, &types.SearchRequest{IndexNames: []string{service.BuildMetadataIndexName(tenant)}, KbIDs: []string{dataset}, Filter: map[string]any{"id": doc}, Limit: 10})
			if err != nil {
				t.Fatal(err)
			}
			return result.Chunks
		}
		rowsBefore := readMeta(cfg.Tenant, a, "doc-a")
		for _, user := range []string{"", "outsider", "inactive"} {
			code := 403
			if user == "" {
				code = 401
			}
			before := counter.writes.Load()
			request(t, "POST", "/api/v1/document/delete_meta", user, `{"doc_id":"doc-a"}`, code)
			if before != counter.writes.Load() {
				t.Fatal("denied metadata deletion reached store")
			}
		}
		before := counter.writes.Load()
		request(t, "POST", "/api/v1/document/delete_meta", "member", `{"doc_id":"doc-private"}`, 403)
		request(t, "POST", "/api/v1/document/delete_meta", "owner", `{"doc_id":"doc-foreign"}`, 403)
		request(t, "POST", "/api/v1/document/set_meta", "member", `{"doc_id":"doc-private","meta":"{\"bad\":1}"}`, 403)
		for _, body := range []string{`{"doc_id":"doc-a","keys":"[]"}`, `{"doc_id":"doc-a","keys":null}`, `{"doc_id":"doc-a","keys":""}`, `{"doc_id":"doc-a","keys":"[\"\"]"}`, `{"doc_id":"doc-a","keys":"[\"rank\",\"rank\"]"}`, `{"doc_id":"doc-a","doc_id":"doc-b"}`} {
			request(t, "POST", "/api/v1/document/delete_meta", "owner", body, 102)
		}
		request(t, "DELETE", "/api/v1/datasets/"+a+"/documents/doc-b/chunks", "owner", `{"delete_all":true}`, 404)
		if before != counter.writes.Load() || !reflect.DeepEqual(readMeta(cfg.Tenant, a, "doc-a"), rowsBefore) {
			t.Fatal("invalid or denied request wrote metadata")
		}
		request(t, "POST", "/api/v1/document/set_meta", "owner", `{"doc_id":"doc-a","meta":"{\"a.b'[]\":\"literal\",\"rank\":0}"}`, 0)
		counter.fail.Store(true)
		request(t, "POST", "/api/v1/document/delete_meta", "owner", `{"doc_id":"doc-a","keys":"[\"rank\"]"}`, 500)
		counter.fail.Store(false)
		if fields, err := serviceMetadataFields(readMeta(cfg.Tenant, a, "doc-a")[0]["meta_fields"]); err != nil || fields["rank"] != float64(0) {
			t.Fatal("failure changed metadata", fields, err)
		}
		binary := os.Getenv("MULTIRAG_GO_DATASET_CHUNK_CLI")
		run := func(input string) {
			t.Helper()
			out, err := exec.CommandContext(ctx, binary, "--host", strings.TrimPrefix(httpServer.URL, "http://"), "--token", tokens["owner"], input).CombinedOutput()
			if err != nil {
				t.Fatalf("CLI %s: %v %s", input, err, out)
			}
			t.Logf("CLI %s: %s", strings.Fields(input)[0], out)
		}
		run(`DELETE METADATA OF DOCUMENT 'doc-a' KEYS '["rank"]';`)
		request(t, "POST", "/api/v1/document/delete_meta", "owner", `{"doc_id":"doc-a","keys":"[\"a.b'[]\"]"}`, 0)
		fields, err := serviceMetadataFields(readMeta(cfg.Tenant, a, "doc-a")[0]["meta_fields"])
		if err != nil || fields["rank"] != nil || fields["a.b'[]"] != nil || fields["enabled"] != false || fields["kind"] != "a" {
			t.Fatal("partial key deletion wrong", fields, err)
		}
		request(t, "POST", "/api/v1/document/delete_meta", "owner", `{"doc_id":"doc-a","keys":"[\"absent\"]"}`, 0)
		request(t, "POST", "/api/v1/document/delete_meta", "owner", `{"doc_id":"doc-a","keys":"[\"enabled\",\"kind\"]"}`, 0)
		if len(readMeta(cfg.Tenant, a, "doc-a")) != 0 {
			t.Fatal("last-key deletion left row")
		}
		run(`DELETE METADATA OF DOCUMENT 'doc-b';`)
		if len(readMeta(cfg.Tenant, cfg.DatasetIDs["b"], "doc-b")) != 0 || len(readMeta(cfg.Tenant, cfg.DatasetIDs["private"], "doc-private")) != 1 || len(readMeta(cfg.Tenant+"_f", cfg.DatasetIDs["foreign"], "doc-foreign")) != 1 {
			t.Fatal("whole-row deletion crossed scope")
		}
		docsAfter := []entity.Document{}
		if err := db.Order("id").Find(&docsAfter).Error; err != nil || !reflect.DeepEqual(docsBefore, docsAfter) {
			t.Fatal("metadata deletion altered SQL documents", err)
		}
	})
	t.Run("canonical_store_cli_and_scope", func(t *testing.T) {
		binary := os.Getenv("MULTIRAG_GO_DATASET_CHUNK_CLI")
		run := func(token, input string) {
			t.Helper()
			out, err := exec.CommandContext(ctx, binary, "--host", strings.TrimPrefix(httpServer.URL, "http://"), "--token", token, input).CombinedOutput()
			if err != nil {
				t.Fatalf("store CLI %s: %v %s", input, err, out)
			}
			t.Logf("store CLI: %s", out)
		}
		if kind == "elasticsearch" {
			run(tokens["owner"], fmt.Sprintf(`CREATE CHUNK STORE FOR DATASET '%s' VECTOR SIZE 2;`, a))
		} else {
			out, err := exec.CommandContext(ctx, binary, "--host", strings.TrimPrefix(httpServer.URL, "http://"), "--token", tokens["owner"], fmt.Sprintf(`CREATE CHUNK STORE FOR DATASET '%s' VECTOR SIZE 2;`, a)).CombinedOutput()
			if err == nil || !strings.Contains(string(out), "already exists") {
				t.Fatalf("Infinity duplicate create changed contract: %v %s", err, out)
			}
		}
		request(t, "POST", "/api/v1/tenant/chunk_store", "outsider", fmt.Sprintf(`{"kb_id":%q,"vector_size":2}`, a), int(common.CodeAuthenticationError))
		run(tokens["owner"], fmt.Sprintf(`DROP CHUNK STORE FOR DATASET '%s';`, a))
		run(tokens["owner"], fmt.Sprintf(`CREATE CHUNK STORE FOR DATASET '%s' VECTOR SIZE 2;`, a))
		result, err := store.Search(ctx, &types.SearchRequest{IndexNames: []string{"multirag_" + cfg.Tenant}, KbIDs: []string{cfg.DatasetIDs["b"]}, IncludeUnavailable: true, Limit: 10})
		if err != nil || len(result.Chunks) != 1 {
			t.Fatal("drop deleted neighbor dataset", result, err)
		}
		email := cfg.Tenant + "@owned.invalid"
		access := strings.Repeat("x", 40)
		if err := db.Create(&entity.User{ID: cfg.Tenant, Email: email, Nickname: "store-owner", AccessToken: &access, Status: &active, IsActive: true, IsAuthenticated: true}).Error; err != nil {
			t.Fatal(err)
		}
		token, err := utility.SignUserJWT(email, server.GetSecretKey())
		if err != nil {
			t.Fatal(err)
		}
		run(token, `DROP METADATA STORE;`)
		if exists, err := store.MetadataStoreExists(ctx, cfg.Tenant); err != nil || exists {
			t.Fatal("metadata store not dropped", exists, err)
		}
		if exists, err := store.MetadataStoreExists(ctx, cfg.Tenant+"_f"); err != nil || !exists {
			t.Fatal("metadata drop crossed tenant", exists, err)
		}
		run(token, `CREATE METADATA STORE;`)
		if exists, err := store.MetadataStoreExists(ctx, cfg.Tenant); err != nil || !exists {
			t.Fatal("metadata store not created", exists, err)
		}
	})

}

// serviceMetadataFields independently decodes stored JSON for acceptance assertions.
func serviceMetadataFields(value any) (map[string]any, error) {
	switch v := value.(type) {
	case map[string]any:
		return v, nil
	case string:
		var result map[string]any
		err := json.Unmarshal([]byte(v), &result)
		return result, err
	}
	return nil, fmt.Errorf("unexpected stored metadata %T", value)
}
