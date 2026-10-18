package service

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"sync/atomic"
	"testing"

	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

// Exercise real catalog loading and the service's instance constructor together.
func TestBaiduOpenAICatalogBoundCapabilitiesHTTP(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	for _, providerName := range []string{"Baidu", "OpenAI"} {
		provider := manager.FindProvider(providerName)
		if provider == nil || provider.URLSuffix.Embedding != "embeddings" {
			t.Fatal("embedding catalog endpoint missing", provider)
		}
		original := provider.URL["default"]
		for _, instance := range []string{"one", "two"} {
			t.Run(providerName+"/"+instance, func(t *testing.T) {
				t.Parallel()
				var calls atomic.Int32
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					calls.Add(1)
					if r.Header.Get("Authorization") != "Bearer "+instance {
						t.Error("credentials crossed instances")
					}
					var body map[string]any
					if r.Method == http.MethodPost {
						if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
							t.Error(err)
						}
					}
					switch r.URL.Path {
					case "/instance/v2/embeddings":
						if body["dimensions"] != float64(2) {
							t.Error("dimensions lost through service binding", body)
						}
						fmt.Fprint(w, `{"data":[{"index":1,"embedding":[1,0]},{"index":0,"embedding":[0,1]}]}`)
					case "/instance/v2/rerank":
						if body["top_n"] != float64(2) {
							t.Error("all-documents top_n default lost", body)
						}
						fmt.Fprint(w, `{"results":[{"index":1,"relevance_score":0.8},{"index":0,"relevance_score":0}]}`)
					case "/instance/v2/chat/completions":
						if body["stream"] == true {
							fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
						} else {
							fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
						}
					case "/instance/v2/models":
						fmt.Fprint(w, `{"data":[{"id":"embedding"}]}`)
					default:
						t.Error("unexpected catalog endpoint", r.URL.Path)
						w.WriteHeader(http.StatusNotFound)
					}
				}))
				defer server.Close()
				extra, err := encodeProviderInstanceExtra("office", server.URL+"/instance/v2/")
				if err != nil {
					t.Fatal(err)
				}
				driver, api, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", APIKey: instance, Extra: extra})
				if err != nil {
					t.Fatal(err)
				}
				name := "embedding-v1"
				if providerName == "OpenAI" {
					name = "text-embedding-3-small"
				}
				bound := models.NewEmbeddingModel(driver, &name, api)
				bound.EmbeddingConfig = &models.EmbeddingConfig{Query: true, Dimension: 2}
				vectors, err := bound.Encode([]string{"a", "b"})
				if err != nil || !reflect.DeepEqual(vectors, [][]float64{{0, 1}, {1, 0}}) {
					t.Fatal(vectors, err)
				}
				if providerName == "Baidu" {
					reranker := "qwen3-reranker-4b"
					scores, err := models.NewRerankModel(driver, &reranker, api).Rerank("q", []string{"a", "b"})
					if err != nil || !reflect.DeepEqual(scores, []float64{0, 0.8}) {
						t.Fatal(scores, err)
					}
				}
				chatName := "deepseek-v3.2"
				chat := models.NewChatModel(driver, &chatName, api)
				messages := []models.Message{{Role: "user", Content: "q"}}
				if _, err := chat.ChatWithMessages(messages, nil); err != nil {
					t.Fatal(err)
				}
				if err := chat.ChatStreamlyWithMessages(messages, nil, func(*string, *string) error { return nil }); err != nil {
					t.Fatal(err)
				}
				if err := driver.CheckConnection(api); err != nil {
					t.Fatal(err)
				}
				expectedCalls := int32(4)
				if providerName == "Baidu" {
					expectedCalls++
				}
				if calls.Load() != expectedCalls || provider.URL["default"] != original || api.Context != nil || bound.EmbeddingConfig.Dimension != 2 || !bound.EmbeddingConfig.Query {
					t.Fatal("instance config mutated or capabilities missing", calls.Load())
				}
				t.Logf("%s/%s: %d authenticated HTTP requests through real catalog and service instance constructor", providerName, instance, calls.Load())
			})
		}
	}
}
