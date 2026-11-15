package handler

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"

	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/service"
	chunkservice "multirag/internal/service/chunk"
	"multirag/internal/service/nlp"
	"multirag/internal/testsupport"
	"multirag/internal/tokenizer"
	"multirag/internal/utility"
)

func TestAfterAABSearchRouterSQLAndElasticsearch(t *testing.T)   { runCanonicalSearchHTTP(t, false) }
func TestAfter5ESearchbotRouterSQLAndElasticsearch(t *testing.T) { runCanonicalSearchHTTP(t, true) }
func runCanonicalSearchHTTP(t *testing.T, searchbot bool) {
	f := newCanonicalHTTPFixture(t)
	store := f.documentEngine(t)
	cfg := server.GetConfig()
	previousDatabase := cfg.Database
	cfg.Database = f.Config.Database
	t.Cleanup(func() { cfg.Database = previousDatabase })
	oldDB := dao.DB
	f.Journal.Event("reserve-canonical-search-schema-migration", map[string]any{"database": f.Database, "operation": "canonical dao.InitDB models/catalog only"})
	if err := dao.InitDB(); err != nil {
		t.Fatal(err)
	}
	pool, err := dao.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close(); dao.DB = oldDB })
	if err := tokenizer.Init(&tokenizer.PoolConfig{DictPath: "resource", MinSize: 1, MaxSize: 2}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(tokenizer.Close)
	if err := nlp.InitQueryBuilderFromTokenizer("resource"); err != nil {
		t.Fatal(err)
	}
	var providerCalls atomic.Int64
	upstream := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		providerCalls.Add(1)
		if r.Header.Get("Authorization") != "Bearer selected-key" || !strings.HasSuffix(r.URL.Path, "/embeddings") {
			t.Error("selected embedding credential/endpoint lost")
		}
		var body struct {
			Model string
			Input []string
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Model != "embed-fixture" || len(body.Input) == 0 {
			t.Error("embedding request", err)
		}
		rows := make([]any, len(body.Input))
		for i := range rows {
			rows[i] = map[string]any{"index": i, "embedding": []float64{1, 0}}
		}
		json.NewEncoder(w).Encode(map[string]any{"data": rows})
	}))
	provider, instance, model := utility.GenerateToken(), utility.GenerateToken(), utility.GenerateToken()
	extra, _ := json.Marshal(map[string]any{"base_url": upstream.URL, "region": "default"})
	for _, row := range []any{&entity.TenantModelProvider{ID: provider, TenantID: f.ids["owner"], ProviderName: "OpenAI"}, &entity.TenantModelInstance{ID: instance, ProviderID: provider, InstanceName: "selected", APIKey: "selected-key", Status: "active", Extra: string(extra)}, &entity.TenantModel{ID: model, ProviderID: provider, InstanceID: instance, ModelName: "embed-fixture", ModelType: "embedding", Status: "active", Extra: `{"max_tokens":2048}`}} {
		if err := f.DB.Create(row).Error; err != nil {
			t.Fatal(err)
		}
	}
	if searchbot {
		legacyName, kind := "embed-fixture@selected", "embedding"
		active := "1"
		if err := f.DB.Create(&entity.LLMFactories{Name: "OpenAI", Tags: "embedding", Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := f.DB.Create(&entity.TenantLLM{TenantID: f.ids["owner"], LLMFactory: "OpenAI", LLMName: &legacyName, ModelType: &kind, Status: "1"}).Error; err != nil {
			t.Fatal(err)
		}
	}
	kb := f.datasets["shared"]
	if err := f.DB.Model(&entity.Knowledgebase{}).Where("id=?", kb).Update("embd_id", "embed-fixture@selected@OpenAI").Error; err != nil {
		t.Fatal(err)
	}
	doc := f.document(t, "shared", "selected source")
	index := "multirag_" + f.ids["owner"]
	if err := store.CreateChunkStore(t.Context(), index, kb, 2, "naive"); err != nil {
		t.Fatal(err)
	}
	rows := make([]map[string]any, 5)
	for i := range rows {
		rows[i] = map[string]any{"id": fmt.Sprintf("selected-%d", i), "kb_id": kb, "doc_id": doc, "docnm_kwd": "selected source", "content_with_weight": "knowledge item", "content_ltks": "knowledge item", "q_2_vec": []float64{1, 0}, "available_int": 1}
	}
	if failed, err := store.InsertChunks(t.Context(), rows, index, kb); err != nil || len(failed) > 0 {
		t.Fatal(failed, err)
	}
	if failed, err := store.InsertMetadata(t.Context(), []map[string]any{{"id": doc, "kb_id": kb, "meta_fields": map[string]any{"large": json.Number("9007199254740993"), "zero": 0, "false": false, "category": "selected"}}}, f.ids["owner"]); err != nil || len(failed) > 0 {
		t.Fatal(failed, err)
	}
	app := f.app()
	path := "/datasets/search"
	if searchbot {
		path = "/searchbots/retrieval_test"
		app.POST(path, NewSearchBotHandler(service.NewSearchService(), service.NewTenantService(), nil, chunkservice.NewChunkService()).RetrievalTest)
	} else {
		app.POST(path, NewDatasetsHandler(service.NewDatasetService(), service.NewMetadataService()).SearchDatasets)
	}
	url := f.serve(t, app)
	body := map[string]any{"question": "knowledge item", "dataset_ids": []string{kb}, "page": 1, "size": 2, "top_k": 5, "similarity_threshold": 0, "vector_similarity_weight": 1}
	if searchbot {
		delete(body, "dataset_ids")
		body["kb_ids"] = []string{kb}
	}
	for _, actor := range []string{"owner", "member"} {
		_, result, raw, _ := f.request(t, url, actor, "POST", path, body)
		if result["code"] != float64(0) {
			if searchbot {
				request := &service.RetrievalTestRequest{Datasets: []string{kb}, Question: "knowledge item"}
				_, cause := chunkservice.NewChunkService().RetrievalTest(request, f.ids[actor])
				t.Log("actual original retrieval cause", cause)
			}
			t.Fatal("canonical search", actor, string(raw))
		}
		chunks := result["data"].(map[string]any)["chunks"].([]any)
		if len(chunks) != 2 {
			t.Fatal("selected page size", result)
		}
		for _, raw := range chunks {
			chunk := raw.(map[string]any)
			if chunk["doc_id"] != doc {
				t.Fatal("retrieval source scope", chunk)
			}
		}
	}
	if !searchbot {
		searchID := utility.GenerateToken()
		status := "1"
		if err := f.DB.Create(&entity.Search{ID: searchID, TenantID: f.ids["owner"], CreatedBy: f.ids["owner"], Name: "saved numeric settings", Status: &status, SearchConfig: entity.JSONMap{"top_k": 1, "similarity_threshold": 2, "vector_similarity_weight": 1}}).Error; err != nil {
			t.Fatal(err)
		}
		body["search_id"] = searchID
		_, saved, _, _ := f.request(t, url, "owner", "POST", path, body)
		if saved["code"] != float64(0) || len(saved["data"].(map[string]any)["chunks"].([]any)) != 0 {
			t.Fatal("SQL saved numeric similarity threshold ignored", saved)
		}

		delete(body, "search_id")
	}
	for _, actor := range []string{"", "invite", "revoked", "foreign", "disabled"} {
		beforeModel := providerCalls.Load()
		_, result, _, _ := f.request(t, url, actor, "POST", path, body)
		if result["code"] == float64(0) || providerCalls.Load() != beforeModel {
			t.Fatal("denied retrieval touched ES/provider", actor, result)
		}
	}
	body["meta_data_filter"] = map[string]any{"method": "manual", "logic": "and", "manual": []any{map[string]any{"key": "category", "op": "is", "value": "missing"}}}
	_, empty, _, _ := f.request(t, url, "owner", "POST", path, body)
	if empty["code"] != float64(0) || len(empty["data"].(map[string]any)["chunks"].([]any)) != 0 {
		t.Fatal("authoritative metadata empty widened scope", empty)
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM tenant_model_instance WHERE id=? AND api_key='selected-key'", instance).Scan(&count); err != nil || count != 1 {
		t.Fatal("independent binding SQL readback", count, err)
	}
	f.Journal.Event("canonical-search-http-mysql-es-readback", map[string]any{"original_searchbot_adapter": searchbot, "actual_es_native_retrieval": true, "controlled_embedding": true, "authorized_page_scope": true, "denied_zero_provider": true, "chunk_zero_engine_io_receipt": "e1519d7d969e49b58f75ff9c0e8b4c15", "manual_metadata_no_match_keeps_empty": true})
}
