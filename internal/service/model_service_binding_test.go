package service

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/service/nlp"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
)

func TestSplitModelInstance(t *testing.T) {
	for _, test := range []struct{ value, name, instance, provider string }{
		{"BAAI/bge@SiliconFlow", "BAAI/bge", "default", "SiliconFlow"},
		{"BAAI/bge@fixture@SiliconFlow", "BAAI/bge", "fixture", "SiliconFlow"},
	} {
		name, instance, provider, err := splitModelInstance(test.value)
		if err != nil || name != test.name || instance != test.instance || provider != test.provider {
			t.Fatalf("split %q = %q %q %q, %v", test.value, name, instance, provider, err)
		}
	}
	for _, value := range []string{"model", "@provider", "model@", "model@@provider", "model@instance@provider@extra"} {
		if _, _, _, err := splitModelInstance(value); err == nil {
			t.Fatalf("accepted %q", value)
		}
	}
}

// Opt-in integration uses only a separately created database with an owned name.
func TestModelBindingScratchPostgres(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_GO_MODELS_DSN")
	if dsn == "" {
		t.Skip("requires an owned model-binding scratch database")
	}
	parsed, err := url.Parse(dsn)
	if err != nil || !strings.HasPrefix(strings.TrimPrefix(parsed.Path, "/"), "multirag_go_models_") {
		t.Fatal("scratch DB name is not owned")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("scratch database connection failed")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	oldDB := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = oldDB })
	if err := db.AutoMigrate(&entity.TenantModelProvider{}, &entity.TenantModelInstance{}, &entity.TenantModel{}, &entity.TenantLLM{}, &entity.Tenant{}); err != nil {
		t.Fatal(err)
	}
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	var paths []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		if r.Header.Get("Authorization") != "Bearer fixture-key" && r.Header.Get("Authorization") != "Bearer fixture-key-VolcEngine" {
			t.Error("wrong tenant key")
		}
		switch r.URL.Path {
		case "/fixture/embeddings", "/legacy/embeddings":
			fmt.Fprint(w, `{"data":[{"index":0,"embedding":[1,2]}]}`)
		case "/fixture/rerank":
			fmt.Fprint(w, `{"results":[{"index":0,"relevance_score":0.8}]}`)
		case "/fixture/chat/completions":
			fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
		default:
			t.Errorf("unexpected URL %s", r.URL.Path)
			w.WriteHeader(404)
		}
	}))
	defer server.Close()
	for _, name := range []string{"SiliconFlow", "VolcEngine"} {
		provider := manager.FindProvider(name)
		provider.URL["fixture"] = server.URL + "/fixture"
		if err := db.Create(&entity.TenantModelProvider{ID: name, TenantID: "tenant", ProviderName: name}).Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Create(&entity.TenantModelInstance{ID: name + "-instance", ProviderID: name, InstanceName: "fixture", APIKey: func() string {
			if name == "VolcEngine" {
				return "fixture-key-VolcEngine"
			}
			return "fixture-key"
		}(), Extra: `{"region":"fixture"}`}).Error; err != nil {
			t.Fatal(err)
		}
	}
	svc := NewModelProviderService()
	svc.providerManager = manager
	embedding, err := svc.GetEmbeddingModel("tenant", "Qwen/Qwen3-Embedding-0.6B@fixture@SiliconFlow")
	if err != nil {
		t.Fatal(err)
	}
	vector, err := embedding.Encode([]string{"q"})
	if err != nil || !reflect.DeepEqual(vector, [][]float64{{1, 2}}) {
		t.Fatalf("vector=%v, error=%v", vector, err)
	}
	queryVectors, err := embedding.Encode([]string{"question"})
	if err != nil || !reflect.DeepEqual(queryVectors, [][]float64{{1, 2}}) {
		t.Fatalf("bound query=%v error=%v", queryVectors, err)
	}
	queryVector := queryVectors[0]
	expression, err := nlp.NewRetrievalService(nil).GetVector("question", embedding, 7, 0.4)
	if err != nil || expression.VectorColumnName != "q_2_vec" || !reflect.DeepEqual(expression.EmbeddingData, queryVector) {
		t.Fatalf("bound retrieval expression=%v error=%v", expression, err)
	}
	rerank, err := svc.GetRerankModel("tenant", "BAAI/bge-reranker-v2-m3@fixture@SiliconFlow")
	if err != nil {
		t.Fatal(err)
	}
	scores, err := rerank.Rerank("q", []string{"doc"})
	if err != nil || !reflect.DeepEqual(scores, []float64{0.8}) {
		t.Fatalf("scores=%v, error=%v", scores, err)
	}
	name := "doubao-seed-2-0-pro-260215@fixture@VolcEngine"
	chat, err := svc.GetChatModel("tenant", name)
	if err != nil {
		t.Fatal(err)
	}
	answer, err := chat.Chat("system", []map[string]string{{"role": "user", "content": "q"}}, nil)
	if err != nil || answer != "answer" {
		t.Fatalf("answer=%q, error=%v", answer, err)
	}
	tenantName, tenantStatus := "fixture", "1"
	if err := db.Create(&entity.Tenant{ID: "tenant", Name: &tenantName, LLMID: name, Status: &tenantStatus}).Error; err != nil {
		t.Fatal(err)
	}
	if _, err := svc.GetChatModel("tenant", ""); err != nil {
		t.Fatalf("default model: %v", err)
	}
	if _, err := svc.GetEmbeddingModel("tenant", name); err == nil {
		t.Fatal("wrong model type accepted")
	}
	if _, err := svc.GetChatModel("other-tenant", name); err == nil {
		t.Fatal("foreign tenant credentials resolved")
	}
	blocked := &entity.TenantModel{ID: "disabled", ProviderID: "VolcEngine", InstanceID: "VolcEngine-instance", ModelName: "doubao-seed-2-0-pro-260215", ModelType: "chat", Status: "disabled"}
	if err := db.Create(blocked).Error; err != nil {
		t.Fatal(err)
	}
	before := len(paths)
	if _, err := svc.GetChatModel("tenant", name); err == nil {
		t.Fatal("disabled model resolved")
	}
	if len(paths) != before {
		t.Fatal("disabled model reached provider")
	}
	// Legacy credentials and a custom URL remain callable after retiring the old factory.
	legacyName, key, base, typ := "legacy-embedding", "fixture-key", server.URL+"/legacy", string(entity.ModelTypeEmbedding)
	if err := db.Create(&entity.TenantLLM{TenantID: "legacy", LLMFactory: "OpenAI", LLMName: &legacyName, APIKey: &key, APIBase: &base, ModelType: &typ, Status: "1"}).Error; err != nil {
		t.Fatal(err)
	}
	legacy, err := svc.GetEmbeddingModel("legacy", legacyName+"@OpenAI")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := legacy.Encode([]string{"q"}); err != nil {
		t.Fatal(err)
	}
	if err := db.Create(&entity.TenantModelProvider{ID: "legacy-openai", TenantID: "legacy", ProviderName: "OpenAI"}).Error; err != nil {
		t.Fatal(err)
	}
	legacy, err = svc.GetEmbeddingModel("legacy", legacyName+"@OpenAI")
	if err != nil {
		t.Fatalf("missing default instance compatibility: %v", err)
	}
	if _, err := legacy.Encode([]string{"q"}); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.GetEmbeddingModel("legacy", legacyName+"@missing@OpenAI"); err == nil {
		t.Fatal("explicit missing instance switched credentials")
	}
	if err := db.Model(&entity.TenantLLM{}).Where("tenant_id = ?", "legacy").Update("mdl_type", nil).Error; err != nil {
		t.Fatal(err)
	}
	if _, err := svc.GetEmbeddingModel("legacy", legacyName+"@OpenAI"); err != nil {
		t.Fatalf("nullable legacy model type: %v", err)
	}
	if err := db.Create(&entity.TenantLLM{TenantID: "custom", LLMFactory: "OpenAI-API-Compatible", LLMName: &legacyName, APIKey: &key, Status: "1"}).Error; err != nil {
		t.Fatal(err)
	}
	if _, err := svc.GetEmbeddingModel("custom", legacyName+"@OpenAI-API-Compatible"); err == nil {
		t.Fatal("missing custom URL accepted")
	}

	if err := db.Exec("ALTER TABLE tenant_model RENAME TO hidden_tenant_model").Error; err != nil {
		t.Fatal(err)
	}
	if _, err := svc.GetEmbeddingModel("tenant", "Qwen/Qwen3-Embedding-0.6B@fixture@SiliconFlow"); err == nil {
		t.Fatal("SQL failure treated as missing disabled row")
	}
	if err := db.Exec("ALTER TABLE hidden_tenant_model RENAME TO tenant_model").Error; err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(paths, []string{"/fixture/embeddings", "/fixture/embeddings", "/fixture/embeddings", "/fixture/rerank", "/fixture/chat/completions", "/legacy/embeddings", "/legacy/embeddings"}) {
		t.Fatalf("paths=%v", paths)
	}
	// Independent SQL readback verifies the persisted denial record.
	var count int
	if err := pool.QueryRow(`SELECT count(*) FROM tenant_model WHERE id='disabled' AND status='disabled'`).Scan(&count); err != nil || count != 1 {
		t.Fatal("disabled state readback failed")
	}
	data, _ := json.Marshal(paths)
	t.Logf("real PostgreSQL + provider HTTP paths: %s", data)
}

func TestEmbeddingUsesResolvedBoundName(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Model string `json:"model"`
		}
		json.NewDecoder(r.Body).Decode(&body)
		if body.Model != "resolved-name" {
			t.Errorf("model=%q", body.Model)
		}
		fmt.Fprint(w, `{"data":[{"index":0,"embedding":[1,2]}]}`)
	}))
	defer server.Close()
	key, name := "key", "resolved-name"
	driver := models.NewSiliconFlowModel(map[string]string{"default": server.URL}, models.URLSuffix{Embedding: "embeddings"})
	bound := models.NewEmbeddingModel(driver, &name, &models.APIConfig{APIKey: &key})
	vectors, err := bound.Encode([]string{"q"})
	if err != nil || !reflect.DeepEqual(vectors, [][]float64{{1, 2}}) {
		t.Fatalf("vectors=%v error=%v", vectors, err)
	}

}
