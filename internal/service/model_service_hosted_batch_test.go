package service

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

func TestModelTypeCapabilityMatrix(t *testing.T) {
	for _, raw := range []string{"chat", "vision", "image2text", "embedding", "rerank", "tts", "asr", "speech2text", "ocr", "", "invalid"} {
		for _, typ := range []entity.ModelType{entity.ModelTypeChat, entity.ModelTypeImage2Text, entity.ModelTypeEmbedding, entity.ModelTypeRerank, entity.ModelTypeTTS, entity.ModelTypeSpeech2Text, entity.ModelTypeOCR} {
			normalized := raw
			if raw == "vision" {
				normalized = "image2text"
			}
			if raw == "asr" {
				normalized = "speech2text"
			}
			want := normalized == string(typ) || typ == entity.ModelTypeChat && normalized == "image2text"
			if modelTypeMatches(raw, typ) != want {
				t.Fatal(raw, typ, want)
			}
		}
	}
}

// Actual SQL-selected credentials, real provider HTTP transport, bound consumers
// and an independent SQL snapshot prove capability denials have no side effects.
func TestHostedProviderBindingScratchPostgres(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_GO_MODELS_DSN")
	if dsn == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned models DB required")
		}
		t.Skip("requires owned models scratch DB")
	}
	u, e := url.Parse(dsn)
	if e != nil || !strings.HasPrefix(strings.TrimPrefix(u.Path, "/"), "multirag_go_models_") {
		t.Fatal("unowned database")
	}
	db, e := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if e != nil {
		t.Fatal("connect owned database")
	}
	pool, e := db.DB()
	if e != nil {
		t.Fatal(e)
	}
	defer pool.Close()
	old := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = old })
	if e := db.AutoMigrate(&entity.TenantModelProvider{}, &entity.TenantModelInstance{}, &entity.TenantModel{}, &entity.TenantLLM{}, &entity.UserTenant{}); e != nil {
		t.Fatal(e)
	}
	readback, e := sql.Open("pgx", dsn)
	if e != nil {
		t.Fatal("readback connect")
	}
	defer readback.Close()
	manager, e := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if e != nil {
		t.Fatal(e)
	}
	svc := NewModelProviderService()
	svc.providerManager = manager
	active := "1"
	for _, id := range []string{"batch-b-owner", "batch-b-other"} {
		if e := db.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: id, Role: "owner", InvitedBy: id, Status: &active}).Error; e != nil {
			t.Fatal(e)
		}
	}
	var calls atomic.Int32
	for _, providerName := range []string{"Mistral", "Jina", "Novita", "LongCat"} {
		t.Run(providerName, func(t *testing.T) {
			provider := manager.FindProvider(providerName)
			if provider == nil {
				t.Fatal("catalog absent")
			}
			original := provider.URL["default"]
			providerID := "batch-b-" + providerName
			if e := db.Create(&entity.TenantModelProvider{ID: providerID, TenantID: "batch-b-owner", ProviderName: providerName}).Error; e != nil {
				t.Fatal(e)
			}
			for i := 0; i < 2; i++ {
				instance := fmt.Sprintf("batch-b-%s-%d", providerName, i)
				fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					calls.Add(1)
					if r.Header.Get("Authorization") != "Bearer "+instance || !strings.HasPrefix(r.URL.Path, "/instance/") {
						t.Error("instance URL/key lost")
					}
					var b map[string]any
					if e := json.NewDecoder(r.Body).Decode(&b); e != nil {
						t.Error(e)
					}
					switch {
					case strings.HasSuffix(r.URL.Path, "embeddings"):
						inputs := b["input"].([]any)
						data := make([]map[string]any, len(inputs))
						for j := range inputs {
							data[len(inputs)-1-j] = map[string]any{"index": j, "embedding": []float64{0, float64(i + 1)}}
						}
						json.NewEncoder(w).Encode(map[string]any{"data": data})
					case strings.HasSuffix(r.URL.Path, "rerank"):
						fmt.Fprint(w, `{"results":[{"index":1,"relevance_score":1},{"index":0,"relevance_score":0}]}`)
					case strings.HasSuffix(r.URL.Path, "chat/completions"):
						if b["stream"] == true {
							fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
						} else {
							fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
						}
					default:
						t.Error("unconfigured path", r.URL.Path)
						w.WriteHeader(404)
					}
				}))
				t.Cleanup(fixture.Close)
				extra, _ := encodeProviderInstanceExtra("office", fixture.URL+"/instance/")
				if e := db.Create(&entity.TenantModelInstance{ID: instance, ProviderID: providerID, InstanceName: fmt.Sprint(i), APIKey: instance, Status: "active", Extra: extra}).Error; e != nil {
					t.Fatal(e)
				}
				for _, definition := range provider.Models {
					composite := definition.Name + "@" + fmt.Sprint(i) + "@" + providerName
					for _, typ := range []entity.ModelType{entity.ModelTypeChat, entity.ModelTypeEmbedding, entity.ModelTypeRerank, entity.ModelTypeSpeech2Text, entity.ModelTypeTTS, entity.ModelTypeOCR} {
						want := false
						for _, raw := range definition.ModelTypes {
							want = want || modelTypeMatches(raw, typ)
						}
						before := calls.Load()
						_, _, _, e := svc.getModelConfig("batch-b-owner", composite, typ)
						if (e == nil) != want || calls.Load() != before {
							t.Fatalf("catalog capability %s %s: %v", composite, typ, e)
						}
					}
				}
				if providerName == "Mistral" || providerName == "Jina" {
					name := "mistral-embed"
					if providerName == "Jina" {
						name = "jina-embeddings-v3"
					}
					r, code, e := svc.EmbedText(providerName, fmt.Sprint(i), name, "batch-b-owner", make([]string, 33), nil, &models.EmbeddingConfig{Query: true, Dimension: 2})
					if e != nil || code != common.CodeSuccess || len(r) != 33 || r[32].Index != 32 || r[32].Embedding[1] != float64(i+1) {
						t.Fatal(r, code, e)
					}
					if providerName == "Jina" {
						bound, e := svc.GetRerankModel("batch-b-owner", "jina-reranker-v3@"+fmt.Sprint(i)+"@Jina")
						if e != nil {
							t.Fatal(e)
						}
						scores, e := bound.Rerank("q", []string{"a", "b"})
						if e != nil || len(scores) != 2 || scores[0] != 0 || scores[1] != 1 {
							t.Fatal(scores, e)
						}
					}
				}
				if providerName != "Jina" {
					name := provider.Models[0].Name
					r, code, e := svc.ChatToModelWithMessages(providerName, fmt.Sprint(i), name, "batch-b-owner", []models.Message{{Role: "system", Content: "rules"}, {Role: "user", Content: "q"}}, nil, nil)
					if e != nil || code != common.CodeSuccess || *r.Answer != "answer" {
						t.Fatal(r, code, e)
					}
					done := 0
					code, e = svc.ChatToModelStreamWithSender(providerName, fmt.Sprint(i), name, "batch-b-owner", "q", nil, nil, func(a, r *string) error {
						if a != nil && *a == "[DONE]" {
							done++
						}
						return nil
					})
					if e != nil || code != common.CodeSuccess || done != 1 {
						t.Fatal(code, e, done)
					}
				}
			}
			if provider.URL["default"] != original {
				t.Fatal("catalog mutated")
			}
		})
	}
	// Custom model capabilities stay explicit, including the normalized vision alias.
	custom := []struct{ name, typ, extra string }{{"vision", "image2text", `{"max_tokens":100,"model_types":["image2text"]}`}, {"multi", "chat", `{"max_tokens":100,"model_types":["chat","embedding"]}`}, {"ocr", "ocr", `{"max_tokens":100,"model_types":["ocr"]}`}, {"tts", "tts", `{"max_tokens":100,"model_types":["tts"]}`}, {"asr", "speech2text", `{"max_tokens":100,"model_types":["speech2text"]}`}, {"old", "rerank", `{"max_tokens":100}`}}
	for _, x := range custom {
		if e := db.Create(&entity.TenantModel{ID: "batch-b-custom-" + x.name, ProviderID: "batch-b-Mistral", InstanceID: "batch-b-Mistral-0", ModelName: x.name, ModelType: x.typ, Status: "active", Extra: x.extra}).Error; e != nil {
			t.Fatal(e)
		}
	}
	if _, e := svc.GetChatModel("batch-b-owner", "vision@0@Mistral"); e != nil {
		t.Fatal("vision chat rejected", e)
	}
	for _, typ := range []entity.ModelType{entity.ModelTypeChat, entity.ModelTypeEmbedding} {
		if _, _, _, e := svc.getModelConfig("batch-b-owner", "multi@0@Mistral", typ); e != nil {
			t.Fatal(e)
		}
	}
	legacyName := "mistral-embed"
	key := "legacy-key"
	base := "http://127.0.0.1:1"
	if e := db.Create(&entity.TenantLLM{TenantID: "batch-b-legacy", LLMFactory: "Mistral", LLMName: &legacyName, APIKey: &key, APIBase: &base, Status: "1"}).Error; e != nil {
		t.Fatal(e)
	}
	if _, e := svc.GetEmbeddingModel("batch-b-legacy", "mistral-embed@Mistral"); e != nil {
		t.Fatal("catalog legacy capability", e)
	}
	snapshot := func() string {
		var value string
		e := readback.QueryRow(`SELECT (SELECT md5(coalesce(string_agg(row_to_json(t)::text,',' ORDER BY id),'')) FROM tenant_model_instance t)||':'||(SELECT md5(coalesce(string_agg(row_to_json(t)::text,',' ORDER BY id),'')) FROM tenant_model t)||':'||(SELECT count(*)::text FROM tenant_model_provider)`).Scan(&value)
		if e != nil {
			t.Fatal(e)
		}
		return value
	}
	before, requests := snapshot(), calls.Load()
	for _, test := range []struct {
		tenant, name string
		typ          entity.ModelType
	}{{"batch-b-owner", "mistral-embed@0@Mistral", entity.ModelTypeChat}, {"batch-b-other", "mistral-embed@0@Mistral", entity.ModelTypeEmbedding}, {"batch-b-owner", "tts@0@Mistral", entity.ModelTypeOCR}, {"batch-b-owner", "ocr@0@Mistral", entity.ModelTypeTTS}, {"batch-b-owner", "vision@0@Mistral", entity.ModelTypeEmbedding}, {"batch-b-legacy", "mistral-embed@Mistral", entity.ModelTypeChat}} {
		if _, _, _, e := svc.getModelConfig(test.tenant, test.name, test.typ); e == nil {
			t.Fatal("invalid capability authorized", test)
		}
	}
	for _, x := range []struct {
		name string
		typ  entity.ModelType
	}{{"ocr", entity.ModelTypeOCR}, {"tts", entity.ModelTypeTTS}, {"asr", entity.ModelTypeSpeech2Text}} {
		var code common.ErrorCode
		var e error
		switch x.typ {
		case entity.ModelTypeOCR:
			_, code, e = svc.OCRFile("Mistral", "0", x.name, "batch-b-owner", []byte("file"), nil, nil)
		case entity.ModelTypeTTS:
			_, code, e = svc.AudioSpeech("Mistral", "0", x.name, "batch-b-owner", "text", nil, nil)
		case entity.ModelTypeSpeech2Text:
			_, code, e = svc.TranscribeAudio("Mistral", "0", x.name, "batch-b-owner", []byte("audio"), nil, nil)
		}
		if code != common.CodeNotImplemented || e == nil {
			t.Fatal("optional media must be 501", x, code, e)
		}
	}
	if snapshot() != before || calls.Load() != requests {
		t.Fatal("denial changed SQL or reached supplier")
	}
	t.Log("two SQL-bound instances per provider, 33-input embeddings, chat/SSE/rerank consumers, catalog/custom/legacy capability checks and independent denial readback passed")
}
