package handler_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"multirag/internal/cli"
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
	"multirag/internal/service/nlp"
	"multirag/internal/testsupport"
	"multirag/internal/tokenizer"
	"multirag/internal/utility"
	"multirag/tests/support"
)

type afterAABSearchEngine struct {
	engine.DocEngine
	searches atomic.Int64
}

func (e *afterAABSearchEngine) Search(ctx context.Context, request *types.SearchRequest) (*types.SearchResult, error) {
	e.searches.Add(1)
	return e.DocEngine.Search(ctx, request)
}

func (e *afterAABSearchEngine) ScanMetadata(ctx context.Context, request *types.SearchRequest, visit func([]map[string]interface{}) error) error {
	e.searches.Add(1)
	scanner, ok := e.DocEngine.(engine.MetadataScanner)
	if !ok {
		return errors.New("metadata snapshot unavailable")
	}
	return scanner.ScanMetadata(ctx, request, visit)
}

// This opt-in test traverses the production Router, auth, SQL selectors, dataset
// orchestration, NLP and the actual ES adapter. Only model HTTP is controlled.
func TestAfterAABSearchRouterSQLAndElasticsearch(t *testing.T) {
	path := os.Getenv("MULTIRAG_AFTERAAB_G_CONFIG")
	if path == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned afteraab G SQL/ES configuration required")
		}
		t.Skip("owned afteraab G SQL/ES configuration required")
	}
	journal := testsupport.NewCase(t)
	recordOwnedOptInTestBinary(t)
	var cfg struct {
		Host, User, Password, Database string
		Port                           int
		Elasticsearch                  server.ElasticsearchConfig
	}
	raw, err := os.ReadFile(path)
	if err != nil || json.Unmarshal(raw, &cfg) != nil || cfg.Host != "127.0.0.1" || cfg.Port != 5432 || cfg.Database != "multirag_afteraab_g_20261029" || cfg.Elasticsearch.Hosts != "http://localhost:1200" {
		t.Fatal("exact pre-registered TCP SQL and borrowed ES endpoints required")
	}
	redact := func(message string) string {
		for _, value := range []string{cfg.Password, cfg.Elasticsearch.Username, cfg.Elasticsearch.Password} {
			if value != "" {
				message = strings.ReplaceAll(message, value, "[redacted]")
			}
		}
		return message
	}
	ctx, cancel := context.WithTimeout(t.Context(), 90*time.Second)
	defer cancel()
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	// The source endpoint payload stays private. Register the derived server
	// configuration before its first write and remove it after use.
	runtimeRoot := os.Getenv("REST_DOCUMENT_RESOURCE_DIR")
	if runtimeRoot == "" {
		t.Fatal("registered runner case directory required")
	}
	configPath := filepath.Join(runtimeRoot, "afteraab-g-server-"+uuid.NewString()+".json")
	configBody, err := json.Marshal(map[string]interface{}{
		"server":     map[string]interface{}{"mode": "release", "secret_key": "afteraab-g-owned-fixture-signing-key-20261029"},
		"database":   map[string]interface{}{"driver": "postgres", "host": cfg.Host, "port": cfg.Port, "database": cfg.Database, "username": cfg.User, "password": cfg.Password, "schema": "public"},
		"doc_engine": map[string]interface{}{"type": "elasticsearch", "es": map[string]interface{}{"hosts": cfg.Elasticsearch.Hosts, "username": cfg.Elasticsearch.Username, "password": cfg.Elasticsearch.Password}},
	})
	if err != nil {
		t.Fatal("encode owned runtime configuration")
	}
	journal.Event("reserve-derived-private-config-before-write", map[string]interface{}{"path": configPath, "sha256": fmt.Sprintf("%x", sha256.Sum256(configBody)), "mode": "0600"})
	configCreated := false
	t.Cleanup(func() {
		if !configCreated {
			return
		}
		if err := os.Remove(configPath); err != nil {
			t.Error("owned runtime config cleanup failed")
		}
		_, statErr := os.Lstat(configPath)
		absent := os.IsNotExist(statErr)
		if !absent {
			t.Error("owned runtime config survived cleanup")
		}
		journal.Event("derived-private-config-cleaned", map[string]interface{}{"path": configPath, "independent_lstat_absent": absent})
	})
	configFile, err := os.OpenFile(configPath, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		t.Fatal("create owned runtime configuration")
	}
	configCreated = true
	_, writeErr := configFile.Write(configBody)
	syncErr, closeErr := configFile.Sync(), configFile.Close()
	if writeErr != nil || syncErr != nil || closeErr != nil {
		t.Fatal("write owned runtime configuration")
	}

	if server.FromConfigFile(configPath) != nil || server.InitVariables(nil) != nil {
		t.Fatal("load owned runtime configuration")
	}
	// InitDB migrates only this already-owned scratch database and loads the
	// current source catalog. It never opens the configured business database.
	journal.Event("reserve-owned-schema-before-InitDB", map[string]interface{}{"host": cfg.Host, "port": cfg.Port, "database": cfg.Database, "schema": "public", "operation": "dao.InitDB current source models and manual migrations"})
	if err := dao.InitDB(); err != nil {
		t.Fatal("owned scratch SQL initialization failed")
	}
	pool, err := dao.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = pool.Close() })
	dsn := url.URL{Scheme: "postgres", User: url.UserPassword(cfg.User, cfg.Password), Host: "127.0.0.1:5432", Path: cfg.Database, RawQuery: "sslmode=disable"}
	read, err := sql.Open("pgx", dsn.String())
	if err != nil {
		t.Fatal("independent SQL connection failed")
	}
	t.Cleanup(func() { _ = read.Close() })
	owner, foreign, actor := "afteraab_g_20261029", "afteraab_g_foreign_20261029", "afteraab-g-reader"
	kb, forbiddenKB := "0000000000001000800000000000aa01", "0000000000001000800000000000aa02"
	index, metadataIndex := "multirag_"+owner, service.BuildMetadataIndexName(owner)
	searchID, documentID := "afteraab-g-saved", "afteraab-g-document"
	active, inactive, embeddingType, rerankType, chatType := "1", "0", "embedding", "rerank", "chat"
	legacyKey, embeddingName, rerankName, chatName := "afteraab-g-legacy-fixture", "afteraab-embedding", "afteraab-rerank", "afteraab-chat"
	journal.Event("reserve-fixture-identities-before-INSERT", map[string]interface{}{"tenant_ids": []string{owner, foreign}, "user_ids": []string{owner, foreign, actor}, "dataset_ids": []string{kb, forbiddenKB}, "document_ids": []string{documentID}, "search_ids": []string{searchID}, "legacy_model_names": []string{embeddingName, rerankName, chatName}, "modern_shadow_ids": []string{"afteraab-g-provider", "afteraab-g-instance", "afteraab-g-shadow-embed", "afteraab-g-shadow-rerank"}, "membership_ids": []string{"afteraab-g-owner-member", "afteraab-g-reader-member"}})
	var preexisting int
	if err := read.QueryRowContext(ctx, "SELECT (SELECT count(*) FROM t_ai_users WHERE id IN ($1,$2,$3)) + (SELECT count(*) FROM t_ai_tenants WHERE id IN ($1,$2)) + (SELECT count(*) FROM t_ai_knowledgebases WHERE id IN ($4,$5)) + (SELECT count(*) FROM t_ai_search WHERE id=$6) + (SELECT count(*) FROM t_ai_documents WHERE id=$7) + (SELECT count(*) FROM tenant_model_provider WHERE id='afteraab-g-provider') + (SELECT count(*) FROM tenant_model_instance WHERE id='afteraab-g-instance') + (SELECT count(*) FROM tenant_model WHERE id IN ('afteraab-g-shadow-embed','afteraab-g-shadow-rerank')) + (SELECT count(*) FROM t_ai_tenant_llms WHERE tenant_id IN ($1,$2)) + (SELECT count(*) FROM t_ai_user_tenants WHERE id IN ('afteraab-g-owner-member','afteraab-g-reader-member'))", owner, foreign, actor, kb, forbiddenKB, searchID, documentID).Scan(&preexisting); err != nil || preexisting != 0 {
		t.Fatal("owned SQL fixture namespace was not absent before use")
	}
	journal.Event("SQL-owned-identities-independent-absent-precheck", map[string]interface{}{"database": cfg.Database, "preexisting": preexisting})
	// All rows use owned IDs. Register cleanup before the first mutation.
	t.Cleanup(func() {
		cleanup, stop := context.WithTimeout(context.Background(), 15*time.Second)
		defer stop()
		deletions := []struct {
			query string
			args  []interface{}
		}{
			{"DELETE FROM t_ai_search WHERE id=$1", []interface{}{searchID}},
			{"DELETE FROM t_ai_documents WHERE id=$1", []interface{}{documentID}},
			{"DELETE FROM t_ai_knowledgebases WHERE id IN ($1,$2)", []interface{}{kb, forbiddenKB}},
			{"DELETE FROM tenant_model WHERE id IN ($1,$2)", []interface{}{"afteraab-g-shadow-embed", "afteraab-g-shadow-rerank"}},
			{"DELETE FROM tenant_model_instance WHERE id=$1", []interface{}{"afteraab-g-instance"}},
			{"DELETE FROM tenant_model_provider WHERE id=$1", []interface{}{"afteraab-g-provider"}},
			{"DELETE FROM t_ai_tenant_llms WHERE tenant_id IN ($1,$2)", []interface{}{owner, foreign}},
			{"DELETE FROM t_ai_user_tenants WHERE id IN ($1,$2)", []interface{}{"afteraab-g-owner-member", "afteraab-g-reader-member"}},
			{"DELETE FROM t_ai_tenants WHERE id IN ($1,$2)", []interface{}{owner, foreign}},
			{"DELETE FROM t_ai_users WHERE id IN ($1,$2,$3)", []interface{}{owner, foreign, actor}},
		}
		for _, deletion := range deletions {
			if _, err := read.ExecContext(cleanup, deletion.query, deletion.args...); err != nil {
				t.Error("owned SQL fixture cleanup failed")
			}
		}
		counts := map[string]int{}
		readbackOK := true
		for _, deletion := range deletions {
			var count int
			query := strings.Replace(deletion.query, "DELETE FROM", "SELECT count(*) FROM", 1)
			if err := read.QueryRowContext(cleanup, query, deletion.args...).Scan(&count); err != nil || count != 0 {
				t.Error("independent SQL cleanup readback failed")
				readbackOK = false
			}
			counts[strings.Fields(deletion.query)[2]] = count
		}
		journal.Event("SQL-cleanup-independent-readback", map[string]interface{}{"database": cfg.Database, "owned_row_counts": counts, "independent_readback_success": readbackOK})
	})
	create := func(value interface{}) {
		t.Helper()
		if err := dao.DB.WithContext(ctx).Create(value).Error; err != nil {
			t.Fatalf("owned fixture INSERT failed: %T: %v", value, err)
		}
	}
	var embeds, reranks, chats, unexpected atomic.Int64
	var modelMode atomic.Int32
	rerankEntered, rerankDrained := make(chan struct{}, 1), make(chan struct{}, 1)
	provider := support.RegisteredHTTPServer(t, http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer "+legacyKey {
			unexpected.Add(1)
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		switch request.URL.Path {
		case "/embeddings":
			embeds.Add(1)
			io.WriteString(w, `{"data":[{"index":0,"embedding":[1,0]}]}`)
		case "/chat/completions":
			chats.Add(1)
			var body struct {
				Model    string `json:"model"`
				Messages []struct {
					Role    string `json:"role"`
					Content string `json:"content"`
				} `json:"messages"`
			}
			if json.NewDecoder(request.Body).Decode(&body) != nil || body.Model != chatName || len(body.Messages) != 2 || body.Messages[0].Role != "system" || !strings.Contains(body.Messages[0].Content, "answer_type_keywords") || !strings.Contains(body.Messages[0].Content, "How are A, B and C connected?") {
				unexpected.Add(1)
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"choices": []interface{}{map[string]interface{}{"message": map[string]interface{}{"content": `{"answer_type_keywords":["person"],"entities_from_query":["A","B","C"]}`}}}})
		case "/rerank":
			reranks.Add(1)
			var body struct {
				Model     string   `json:"model"`
				Query     string   `json:"query"`
				Documents []string `json:"documents"`
			}
			if json.NewDecoder(request.Body).Decode(&body) != nil || body.Model != rerankName || len(body.Documents) == 0 {
				unexpected.Add(1)
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			if modelMode.Load() == 1 {
				w.WriteHeader(http.StatusBadGateway)
				return
			}
			if modelMode.Load() == 2 {
				io.WriteString(w, `{"results":[]}`)
				return
			}
			if modelMode.Load() == 3 {
				rerankEntered <- struct{}{}
				<-request.Context().Done()
				rerankDrained <- struct{}{}
				return
			}
			results := make([]map[string]interface{}, 0, len(body.Documents))
			for i, content := range body.Documents {
				number, err := strconv.Atoi(strings.TrimPrefix(content, "knowledge item "))
				if err != nil {
					unexpected.Add(1)
					w.WriteHeader(http.StatusBadRequest)
					return
				}
				results = append(results, map[string]interface{}{"index": i, "relevance_score": float64(70 - number)})
			}
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"results": results})
		default:
			unexpected.Add(1)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	for _, id := range []string{owner, foreign, actor} {
		create(&entity.User{ID: id, Nickname: id, Email: id + "@owned.invalid", Status: &active})
	}
	create(&entity.Tenant{ID: owner, Status: &active})
	create(&entity.Tenant{ID: foreign, Status: &active})
	create(&entity.UserTenant{ID: "afteraab-g-owner-member", UserID: owner, TenantID: owner, Role: "owner", Status: &active})
	create(&entity.UserTenant{ID: "afteraab-g-reader-member", UserID: actor, TenantID: owner, Role: "normal", Status: &active})
	baseURL := provider.URL
	embedding := &entity.TenantLLM{TenantID: owner, LLMFactory: "SiliconFlow", LLMName: &embeddingName, ModelType: &embeddingType, APIKey: &legacyKey, APIBase: &baseURL, Status: active}
	reranker := &entity.TenantLLM{TenantID: owner, LLMFactory: "SiliconFlow", LLMName: &rerankName, ModelType: &rerankType, APIKey: &legacyKey, APIBase: &baseURL, Status: active}
	create(embedding)
	create(reranker)
	chat := &entity.TenantLLM{TenantID: owner, LLMFactory: "SiliconFlow", LLMName: &chatName, ModelType: &chatType, APIKey: &legacyKey, APIBase: &baseURL, Status: active}
	create(chat)
	if err := dao.DB.WithContext(ctx).Model(&entity.Tenant{}).Where("id = ?", owner).Update("tenant_llm_id", chat.ID).Error; err != nil {
		t.Fatal("set owned numeric default chat binding failed")
	}
	// Same-name modern rows must never replace an explicit numeric legacy binding.
	create(&entity.TenantModelProvider{ID: "afteraab-g-provider", TenantID: owner, ProviderName: "SiliconFlow"})
	extra, _ := json.Marshal(map[string]string{"base_url": provider.URL})
	create(&entity.TenantModelInstance{ID: "afteraab-g-instance", ProviderID: "afteraab-g-provider", InstanceName: "default", APIKey: "wrong-modern-key", Status: "active", Extra: string(extra)})
	for _, row := range []entity.TenantModel{{ID: "afteraab-g-shadow-embed", ProviderID: "afteraab-g-provider", InstanceID: "afteraab-g-instance", ModelName: embeddingName, ModelType: embeddingType, Status: "active"}, {ID: "afteraab-g-shadow-rerank", ProviderID: "afteraab-g-provider", InstanceID: "afteraab-g-instance", ModelName: rerankName, ModelType: rerankType, Status: "active"}} {
		create(&row)
	}
	create(&entity.Knowledgebase{ID: kb, Name: "owned shared", TenantID: owner, CreatedBy: owner, Permission: "team", EmbdID: embeddingName + "@SiliconFlow", TenantEmbdID: &embedding.ID, Status: &active})
	create(&entity.Knowledgebase{ID: forbiddenKB, TenantID: foreign, CreatedBy: foreign, Permission: "me", Status: &active})
	create(&entity.Document{ID: documentID, KbID: kb, CreatedBy: owner, ParserID: "naive", Type: "txt", Suffix: "txt", ParserConfig: entity.JSONMap{}, MetaFields: &entity.JSONMap{}, Name: func() *string { name := "owned document"; return &name }(), Status: &active})
	savedConfig := entity.JSONMap{"tenant_rerank_id": reranker.ID, "similarity_threshold": 0, "vector_similarity_weight": 1, "top_k": 1024, "keyword": false, "use_kg": false, "cross_languages": []string{}}
	create(&entity.Search{ID: searchID, TenantID: owner, CreatedBy: owner, Name: "owned search", Status: &active, SearchConfig: savedConfig})
	var readID int64
	if err := read.QueryRowContext(ctx, "SELECT (search_config->>'tenant_rerank_id')::bigint FROM t_ai_search WHERE id=$1", searchID).Scan(&readID); err != nil || readID != reranker.ID {
		t.Fatal("saved numeric model identity independent SQL readback failed")
	}
	journal.Event("saved-numeric-selector-independent-readback", map[string]interface{}{"search_id": searchID, "tenant_rerank_id": readID, "embedding_id": embedding.ID})
	if err := tokenizer.Init(&tokenizer.PoolConfig{DictPath: "resource", MinSize: 1, MaxSize: 2}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(tokenizer.Close)
	if err := nlp.InitQueryBuilderFromTokenizer("resource"); err != nil {
		t.Fatal(err)
	}
	if err := engine.Init(&server.DocEngineConfig{Type: server.EngineElasticsearch, ES: &cfg.Elasticsearch}); err != nil {
		message := err.Error()
		for _, value := range []string{cfg.Elasticsearch.Username, cfg.Elasticsearch.Password} {
			if value != "" {
				message = strings.ReplaceAll(message, value, "[redacted]")
			}
		}
		t.Fatal("borrowed ES adapter initialization failed:", message)
	}
	t.Cleanup(func() { _ = engine.Close() })
	store := engine.Get()
	client := &http.Client{Timeout: 10 * time.Second}
	t.Cleanup(client.CloseIdleConnections)
	es := func(callctx context.Context, method, suffix string) *http.Response {
		t.Helper()
		request, err := http.NewRequestWithContext(callctx, method, cfg.Elasticsearch.Hosts+suffix, nil)
		if err != nil {
			t.Fatal(err)
		}
		request.SetBasicAuth(cfg.Elasticsearch.Username, cfg.Elasticsearch.Password)
		response, err := client.Do(request)
		if err != nil {
			t.Fatal("independent ES readback failed")
		}
		return response
	}
	journal.Event("reserve-owned-ES-indices-before-create", map[string]interface{}{"endpoint": cfg.Elasticsearch.Hosts, "indices": []string{index, metadataIndex}, "dataset_ids": []string{kb}, "document_ids": []string{documentID}, "chunk_ids": "afteraab-g-chunk-000 through afteraab-g-chunk-069"})
	for _, owned := range []string{index, metadataIndex} {
		response := es(ctx, http.MethodHead, "/"+owned)
		response.Body.Close()
		if response.StatusCode != http.StatusNotFound {
			t.Fatal("owned ES index already exists")
		}
	}
	t.Cleanup(func() {
		cleanup, stop := context.WithTimeout(context.Background(), 15*time.Second)
		defer stop()
		for _, owned := range []string{index, metadataIndex} {
			for _, method := range []string{http.MethodDelete, http.MethodHead} {
				request, err := http.NewRequestWithContext(cleanup, method, cfg.Elasticsearch.Hosts+"/"+owned, nil)
				if err != nil {
					t.Error("owned ES cleanup request failed")
					continue
				}
				request.SetBasicAuth(cfg.Elasticsearch.Username, cfg.Elasticsearch.Password)
				response, err := client.Do(request)
				if err != nil {
					t.Error("owned ES cleanup transport failed")
					continue
				}
				response.Body.Close()
				if method == http.MethodHead {
					if response.StatusCode != http.StatusNotFound {
						t.Error("owned ES index survived cleanup")
					}
					journal.Event("ES-cleanup-independent-HEAD", map[string]interface{}{"index": owned, "status": response.StatusCode})
				} else if response.StatusCode != http.StatusOK && response.StatusCode != http.StatusNotFound {
					t.Error("owned ES index deletion failed")
				}
			}
		}
	})
	if err := store.CreateChunkStore(ctx, index, kb, 2, "naive"); err != nil {
		t.Fatal("owned chunk store creation failed")
	}
	rows := make([]map[string]interface{}, 70)
	for i := range rows {
		content := fmt.Sprintf("knowledge item %03d", i)
		rows[i] = map[string]interface{}{"id": fmt.Sprintf("afteraab-g-chunk-%03d", i), "kb_id": kb, "doc_id": documentID, "docnm_kwd": "owned document", "content_with_weight": content, "content_ltks": "knowledge item", "q_2_vec": []float64{1, 0}, "available_int": 1}
	}
	rowInput, _ := json.Marshal(rows)
	journal.Event("reserve-chunk-input-before-INSERT", map[string]interface{}{"index": index, "row_count": len(rows), "input_sha256": fmt.Sprintf("%x", sha256.Sum256(rowInput))})
	if failed, err := store.InsertChunks(ctx, rows, index, kb); err != nil || len(failed) != 0 {
		t.Fatal("owned chunk INSERT failed")
	}
	if failed, err := store.InsertMetadata(ctx, []map[string]interface{}{{"id": documentID, "kb_id": kb, "meta_fields": map[string]interface{}{"large": json.Number("9007199254740993"), "zero": 0, "false": false}}}, owner); err != nil || len(failed) != 0 {
		t.Fatal("owned metadata INSERT failed")
	}
	response := es(ctx, http.MethodPost, "/"+index+"/_refresh")
	response.Body.Close()
	response = es(ctx, http.MethodGet, "/"+index+"/_count")
	var counted struct{ Count int }
	decodeErr := json.NewDecoder(response.Body).Decode(&counted)
	response.Body.Close()
	if decodeErr != nil || counted.Count != 70 {
		t.Fatal("independent ES chunk count readback failed")
	}
	journal.Event("ES-chunk-count-independent-readback", map[string]interface{}{"index": index, "count": counted.Count})
	counter := &afterAABSearchEngine{DocEngine: store}
	users := service.NewUserService()
	chunks := handler.NewChunkHandler(service.NewChunkService(counter), users)
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.Use(gin.Recovery())
	priorAdmin := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned afteraab search acceptance")
	t.Cleanup(func() { local.SetAdminStatus(priorAdmin.Status, priorAdmin.Reason) })
	router.NewRouter(handler.NewAuthHandler(), nil, nil, nil, handler.NewDatasetsHandler(service.NewDatasetsService(), counter), nil, nil, chunks, nil, nil, nil, nil, nil, nil, nil, nil).Setup(app)
	api := support.RegisteredHTTPServer(t, app)
	token, err := utility.SignUserJWT(actor+"@owned.invalid", server.GetSecretKey())
	if err != nil {
		t.Fatal(err)
	}
	type envelope struct {
		Code    int                           `json:"code"`
		Data    service.RetrievalTestResponse `json:"data"`
		Message string                        `json:"message"`
	}
	call := func(callctx context.Context, body map[string]interface{}) (int, envelope, error) {
		data, err := json.Marshal(body)
		if err != nil {
			return 0, envelope{}, err
		}
		request, err := http.NewRequestWithContext(callctx, http.MethodPost, api.URL+"/api/v1/datasets/search", strings.NewReader(string(data)))
		if err != nil {
			return 0, envelope{}, err
		}
		request.Header.Set("Authorization", "Bearer "+token)
		request.Header.Set("Content-Type", "application/json")
		response, err := api.Client().Do(request)
		if err != nil {
			return 0, envelope{}, err
		}
		defer response.Body.Close()
		var result envelope
		if err := json.NewDecoder(response.Body).Decode(&result); err != nil {
			return response.StatusCode, result, err
		}
		return response.StatusCode, result, nil
	}
	baseRequest := func() map[string]interface{} {
		return map[string]interface{}{"dataset_ids": []string{kb}, "question": "knowledge", "tenant_rerank_id": reranker.ID, "size": 10, "top_k": 1024, "similarity_threshold": 0, "vector_similarity_weight": 1}
	}
	request := baseRequest()
	request["page"] = 7
	status, result, err := call(ctx, request)
	if err != nil || status != http.StatusOK || result.Code != 0 || len(result.Data.Chunks) != 10 || result.Data.Total != 70 {
		t.Fatalf("real page7 retrieval failed: status=%d code=%d chunks=%d total=%d err=%v", status, result.Code, len(result.Data.Chunks), result.Data.Total, err)
	}
	for i, chunk := range result.Data.Chunks {
		if chunk["chunk_id"] != fmt.Sprintf("afteraab-g-chunk-%03d", 60+i) {
			t.Fatal("page7 did not preserve the aligned rerank window", chunk["chunk_id"])
		}
	}
	request = baseRequest()
	request["search_id"], request["tenant_rerank_id"], request["similarity_threshold"], request["keyword"], request["cross_languages"] = searchID, int64(999999), 0.99, true, []string{"French"}
	status, result, err = call(ctx, request)
	if err != nil || status != 200 || result.Code != 0 || len(result.Data.Chunks) != 10 || result.Data.Total != 70 || unexpected.Load() != 0 || chats.Load() != 0 {
		t.Fatal("saved numeric/zero/false/empty-language controls were not applied", status, result.Code, err)
	}
	manual := baseRequest()
	manual["meta_data_filter"] = map[string]interface{}{"method": "manual", "manual": []interface{}{map[string]interface{}{"key": "large", "op": "=", "value": json.Number("9007199254740993")}}}
	status, result, err = call(ctx, manual)
	if err != nil || status != 200 || result.Code != 0 || len(result.Data.Chunks) != 10 {
		t.Fatal("actual metadata HTTP/ES exact operand path failed", status, result.Code, redact(result.Message), err)
	}
	beforeFilteredEmbed, beforeFilteredRerank := embeds.Load(), reranks.Load()
	manual["meta_data_filter"] = map[string]interface{}{"method": "manual", "manual": []interface{}{map[string]interface{}{"key": "large", "op": "=", "value": json.Number("9007199254740992")}}}
	status, result, err = call(ctx, manual)
	if err != nil || status != 200 || result.Code != 0 || result.Data.Total != 0 || embeds.Load() != beforeFilteredEmbed || reranks.Load() != beforeFilteredRerank {
		t.Fatal("metadata no-match broadened the selection or invoked a model", status, result.Code, err)
	}
	request = baseRequest()
	request["similarity_threshold"], request["size"] = 0.9, 30
	status, result, err = call(ctx, request)
	if err != nil || status != 200 || result.Code != 0 || len(result.Data.Chunks) != 7 || result.Data.Chunks[0]["vector_similarity"] != float64(1) {
		t.Fatal("normalized logits and threshold disagree", status, result.Code, len(result.Data.Chunks), err)
	}
	assertNoIO := func(body map[string]interface{}, wantSuccess bool) {
		t.Helper()
		beforeEmbed, beforeRerank, beforeChat, beforeUnexpected, beforeIndex := embeds.Load(), reranks.Load(), chats.Load(), unexpected.Load(), counter.searches.Load()
		_, result, err := call(ctx, body)
		if err != nil || wantSuccess && (result.Code != 0 || result.Data.Total != 0 || len(result.Data.Chunks) != 0) || !wantSuccess && result.Code == 0 {
			t.Fatal("early authorization/empty boundary returned wrong result", result.Code, err)
		}
		if embeds.Load() != beforeEmbed || reranks.Load() != beforeRerank || chats.Load() != beforeChat || unexpected.Load() != beforeUnexpected || counter.searches.Load() != beforeIndex {
			t.Fatal("early selector boundary performed provider or index IO")
		}
	}
	savedEmpty := entity.JSONMap{"tenant_rerank_id": reranker.ID, "similarity_threshold": 0, "vector_similarity_weight": 0, "top_k": 0, "keyword": false, "use_kg": false, "cross_languages": []string{}, "doc_ids": []string{}}
	if err := dao.DB.Model(&entity.Search{}).Where("id = ?", searchID).Update("search_config", savedEmpty).Error; err != nil {
		t.Fatal(err)
	}
	request = baseRequest()
	request["search_id"], request["doc_ids"], request["keyword"], request["use_kg"] = searchID, []string{documentID}, true, true
	assertNoIO(request, true)
	if err := dao.DB.Model(&entity.Search{}).Where("id = ?", searchID).Update("search_config", savedConfig).Error; err != nil {
		t.Fatal(err)
	}
	request = baseRequest()
	request["doc_ids"] = []string{}
	assertNoIO(request, true)
	request["tenant_rerank_id"] = int64(999999)
	assertNoIO(request, false)
	request = baseRequest()
	request["dataset_ids"] = []string{kb, forbiddenKB}
	assertNoIO(request, false)
	for _, table := range []string{"t_ai_tenants", "t_ai_users"} {
		if err := dao.DB.Table(table).Where("id = ?", owner).Update("status", inactive).Error; err != nil {
			t.Fatal(err)
		}
		assertNoIO(baseRequest(), false)
		if err := dao.DB.Table(table).Where("id = ?", owner).Update("status", active).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := dao.DB.Model(&entity.TenantLLM{}).Where("id = ?", embedding.ID).Update("status", inactive).Error; err != nil {
		t.Fatal(err)
	}
	request = baseRequest()
	request["doc_ids"] = []string{}
	assertNoIO(request, false)
	if err := dao.DB.Model(&entity.TenantLLM{}).Where("id = ?", embedding.ID).Update("status", active).Error; err != nil {
		t.Fatal(err)
	}
	if err := dao.DB.Model(&entity.TenantLLM{}).Where("id = ?", reranker.ID).Update("status", inactive).Error; err != nil {
		t.Fatal(err)
	}
	request = baseRequest()
	request["doc_ids"] = []string{}
	assertNoIO(request, false)
	if err := dao.DB.Model(&entity.TenantLLM{}).Where("id = ?", reranker.ID).Update("status", active).Error; err != nil {
		t.Fatal(err)
	}
	// KG model selectors are preflighted before even ordinary retrieval IO.
	request = baseRequest()
	request["use_kg"] = true
	if err := dao.DB.Model(&entity.TenantLLM{}).Where("id = ?", chat.ID).Update("status", inactive).Error; err != nil {
		t.Fatal(err)
	}
	assertNoIO(request, false)
	if err := dao.DB.Model(&entity.TenantLLM{}).Where("id = ?", chat.ID).Update("status", active).Error; err != nil {
		t.Fatal(err)
	}
	if err := dao.DB.Model(&entity.Tenant{}).Where("id = ?", owner).Updates(map[string]interface{}{"tenant_llm_id": nil, "llm_id": ""}).Error; err != nil {
		t.Fatal(err)
	}
	assertNoIO(request, false)
	if err := dao.DB.Model(&entity.Tenant{}).Where("id = ?", owner).Update("tenant_llm_id", chat.ID).Error; err != nil {
		t.Fatal(err)
	}
	journal.Event("KG-default-chat-preflight-zero-IO-readback", map[string]interface{}{"owner_id": owner, "numeric_chat_id": chat.ID, "disabled_default_rejected": true, "missing_default_rejected": true, "provider_index_delta": 0})

	// The actual CLI resolves the dataset name through this Router before its
	// parser -> Execute dispatcher posts the full options payload to search.
	address, err := url.Parse(api.URL)
	if err != nil {
		t.Fatal(err)
	}
	cliPort, err := strconv.Atoi(address.Port())
	if err != nil {
		t.Fatal(err)
	}
	consumer := cli.NewMultiRAGClient("user")
	consumer.HTTPClient.Host, consumer.HTTPClient.Port, consumer.HTTPClient.LoginToken = address.Hostname(), cliPort, token
	statement := fmt.Sprintf(`SEARCH 'knowledge' ON DATASETS 'owned shared' WITH page 7 page_size 10 top_k 1024 similarity_threshold 0 vector_similarity_weight 1 tenant_rerank_id %d keyword false use_kg false cross_languages [] meta_data_filter '{"method":"manual","manual":[{"key":"large","op":"=","value":"9007199254740993"}]}';`, reranker.ID)
	command, err := cli.NewParser(statement).Parse(false)
	if err != nil {
		t.Fatal("actual CLI options parse failed", err)
	}
	cliResult, err := consumer.ExecuteCommand(command)
	if err != nil {
		t.Fatal("actual CLI dataset search failed", redact(err.Error()))
	}
	cliRows, ok := cliResult.(*cli.CommonResponse)
	if !ok || cliRows.Code != 0 || len(cliRows.Data) != 10 {
		t.Fatal("actual CLI page7 result shape failed")
	}
	for i, row := range cliRows.Data {
		if row["id"] != fmt.Sprintf("afteraab-g-chunk-%03d", 60+i) || row["dataset_id"] != kb {
			t.Fatal("actual CLI page7 lost the filtered ordered result")
		}
	}
	journal.Event("actual-CLI-parser-Execute-Router-readback", map[string]interface{}{"page": 7, "size": 10, "rows": len(cliRows.Data), "dataset_id": kb, "numeric_rerank_id": reranker.ID, "metadata_exact_string_operand": "9007199254740993", "auth_channel": "api"})

	// Consume the frozen Python graph producer's actual output. Only the
	// authorized KB binding changes; pagerank, N-hop, edge weights, vectors and
	// community content remain the producer's original fields.
	producerPath := os.Getenv("MULTIRAG_AFTERAAB_KG_PRODUCER")
	if producerPath == "" {
		producerPath = filepath.Clean(filepath.Join(filepath.Dir(path), "..", "K", "producer-output-v2.json"))
	}
	producerRaw, err := os.ReadFile(producerPath)
	if err != nil || fmt.Sprintf("%x", sha256.Sum256(producerRaw)) != "b3b3ccaa05bc5067e203c707d8af60818fd3f696e3f67224ba857e89ea5e495b" {
		t.Fatal("frozen KG producer output unavailable or changed")
	}
	var producer struct {
		Producer string                   `json:"producer"`
		Chunks   []map[string]interface{} `json:"chunks"`
	}
	decoder := json.NewDecoder(bytes.NewReader(producerRaw))
	decoder.UseNumber()
	if decoder.Decode(&producer) != nil || len(producer.Chunks) != 6 || producer.Producer == "" {
		t.Fatal("invalid KG producer output")
	}
	graphRows, expectedGraph := []map[string]interface{}{}, map[string]map[string]interface{}{}
	kinds := map[string]int{}
	for _, original := range producer.Chunks {
		row := make(map[string]interface{}, len(original))
		for key, value := range original {
			row[key] = value
		}
		id, idOK := row["id"].(string)
		kind, kindOK := row["knowledge_graph_kwd"].(string)
		if !idOK || id == "" || !kindOK || row["available_int"] != json.Number("0") {
			t.Fatal("invalid producer graph identity/visibility")
		}
		if vector, present := row["q_2_vec"]; present {
			values, valid := vector.([]interface{})
			if !valid || len(values) != 2 {
				t.Fatal("producer vector dimension differs from actual embedding")
			}
		}
		row["kb_id"] = kb
		graphRows = append(graphRows, row)
		expectedGraph[id] = row
		kinds[kind]++
	}
	if kinds["entity"] != 3 || kinds["relation"] != 2 || kinds["community_report"] != 1 {
		t.Fatal("incomplete producer graph kinds")
	}
	graphInput, _ := json.Marshal(graphRows)
	journal.Event("reserve-producer-graph-before-real-ES-INSERT", map[string]interface{}{"input_path": producerPath, "input_sha256": fmt.Sprintf("%x", sha256.Sum256(producerRaw)), "bound_input_sha256": fmt.Sprintf("%x", sha256.Sum256(graphInput)), "producer": producer.Producer, "index": index, "dataset_id": kb, "row_kinds": kinds, "adaptation": "only kb_id binding; preserve original rank/hop/weight/vector/community"})
	if failed, err := store.InsertChunks(ctx, graphRows, index, kb); err != nil || len(failed) != 0 {
		t.Fatal("actual producer graph INSERT failed")
	}
	graphQuery, _ := json.Marshal(map[string]interface{}{"size": 10, "query": map[string]interface{}{"exists": map[string]interface{}{"field": "knowledge_graph_kwd"}}})
	graphRequest, err := http.NewRequestWithContext(ctx, http.MethodPost, cfg.Elasticsearch.Hosts+"/"+index+"/_search", bytes.NewReader(graphQuery))
	if err != nil {
		t.Fatal(err)
	}
	graphRequest.SetBasicAuth(cfg.Elasticsearch.Username, cfg.Elasticsearch.Password)
	graphRequest.Header.Set("Content-Type", "application/json")
	graphResponse, err := client.Do(graphRequest)
	if err != nil {
		t.Fatal("independent producer graph ES readback transport failed")
	}
	var storedGraph struct {
		Hits struct {
			Total struct {
				Value int `json:"value"`
			} `json:"total"`
			Hits []struct {
				Source map[string]interface{} `json:"_source"`
			} `json:"hits"`
		} `json:"hits"`
	}
	graphDecoder := json.NewDecoder(graphResponse.Body)
	graphDecoder.UseNumber()
	graphErr := graphDecoder.Decode(&storedGraph)
	graphResponse.Body.Close()
	if graphErr != nil || graphResponse.StatusCode != 200 || storedGraph.Hits.Total.Value != 6 || len(storedGraph.Hits.Hits) != 6 {
		t.Fatal("independent producer graph ES row count failed")
	}
	for _, hit := range storedGraph.Hits.Hits {
		id, _ := hit.Source["id"].(string)
		if !reflect.DeepEqual(hit.Source, expectedGraph[id]) {
			t.Fatal("producer fields changed during real storage", id)
		}
	}
	beforeChat := chats.Load()
	request = baseRequest()
	request["question"], request["use_kg"] = "How are A, B and C connected?", true
	status, result, err = call(ctx, request)
	if err != nil || status != 200 || result.Code != 0 || result.Data.Total != 70 || len(result.Data.Chunks) != 11 {
		t.Fatal("actual producer -> ES -> Router use_kg failed", status, result.Code, redact(result.Message), err)
	}
	graph := result.Data.Chunks[0]
	content, _ := graph["content_with_weight"].(string)
	if graph["knowledge_graph"] != true || graph["kb_id"] != kb || graph["doc_id"] != "" || graph["vector_similarity"] != nil || graph["term_similarity"] != nil || chats.Load() != beforeChat+1 {
		t.Fatal("KG aggregate lost scope, synthetic score or single rewrite binding")
	}
	for _, expected := range []string{"---- Entities ----", "---- Relations ----", "A knows B", "B knows C", "ABC community 1", "B connects A and C"} {
		if !strings.Contains(content, expected) {
			t.Fatal("actual KG pipeline omitted producer content", expected)
		}
	}
	journal.Event("actual-producer-KG-consumer-independent-readback", map[string]interface{}{"producer_input_sha256": fmt.Sprintf("%x", sha256.Sum256(producerRaw)), "bound_graph_rows": 6, "original_rank_hop_vector_fields_equal": true, "index": index, "dataset_id": kb, "query_rewrite_http_delta": 1, "ordinary_total": result.Data.Total, "response_chunks": len(result.Data.Chunks), "aggregate_content_sha256": fmt.Sprintf("%x", sha256.Sum256([]byte(content))), "synthetic_score": true, "document_identity_claimed": false})

	for _, mode := range []int32{1, 2} {
		modelMode.Store(mode)
		_, result, err = call(ctx, baseRequest())
		if err != nil || result.Code == 0 {
			t.Fatal("provider/shape error became successful zero scores", mode, result.Code, err)
		}
	}
	modelMode.Store(3)
	canceled, stop := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() { _, _, err := call(canceled, baseRequest()); done <- err }()
	select {
	case <-rerankEntered:
		stop()
	case <-time.After(5 * time.Second):
		stop()
		t.Fatal("slow rerank was not entered")
	}
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatal("HTTP cancellation was lost", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("HTTP cancellation did not drain")
	}
	select {
	case <-rerankDrained:
	case <-time.After(5 * time.Second):
		t.Fatal("canceled provider request did not drain")
	}
	modelMode.Store(0)
	if unexpected.Load() != 0 {
		t.Fatal("controlled provider observed unexpected credentials, model or input")
	}
	journal.Event("real-search-consumer-boundaries", map[string]interface{}{"router": "NewRouter datasets.POST /search", "page": 7, "size": 10, "window": 70, "total": 70, "saved_numeric_id": reranker.ID, "saved_zero_false_empty_controls": true, "early_empty_and_forbidden_provider_index_delta": 0, "normalized_threshold_count": 7, "provider_error_and_malformed_result_propagated": true, "provider_cancellation_drained": true, "provider_http_calls": embeds.Load() + reranks.Load() + chats.Load(), "index_search_calls": counter.searches.Load()})
}
