package service

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

// Load the real catalogs, resolve two independent service instances, and run
// the bound chat/vector consumers against authenticated HTTP provider fixtures.
func TestAddedProviderCatalogBoundConsumersHTTP(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	for _, providerName := range []string{"CoHere", "FishAudio", "StepFun", "Baichuan", "Upstage"} {
		provider := manager.FindProvider(providerName)
		if provider == nil {
			t.Fatal("missing catalog", providerName)
		}
		originalURL := provider.URL["default"]
		for _, instance := range []string{"one", "two"} {
			t.Run(providerName+"/"+instance, func(t *testing.T) {
				t.Parallel()
				var calls atomic.Int32
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					calls.Add(1)
					if r.Header.Get("Authorization") != "Bearer "+instance {
						t.Error("credentials crossed instances")
					}
					if !strings.HasPrefix(r.URL.Path, "/instance/") {
						t.Error("instance URL ignored", r.URL.Path)
					}
					path := strings.TrimPrefix(r.URL.Path, "/instance/")
					var body map[string]any
					if r.Method == http.MethodPost {
						if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
							t.Error(err)
						}
					}
					switch path {
					case "v2/chat", "chat/completions":
						if body["stream"] == true {
							if providerName == "CoHere" {
								fmt.Fprint(w, "data: {\"type\":\"content-delta\",\"delta\":{\"message\":{\"content\":{\"text\":\"answer\"}}}}\n\ndata: {\"type\":\"message-end\"}\n\n")
							} else {
								fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
							}
						} else if providerName == "CoHere" {
							fmt.Fprint(w, `{"message":{"content":[{"type":"text","text":"answer"}]}}`)
						} else {
							fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
						}
					case "v2/embed":
						if body["input_type"] != "search_query" || body["output_dimension"] != float64(2) {
							t.Error("Cohere config lost", body)
						}
						fmt.Fprint(w, `{"embeddings":{"float":[[0,1],[1,0]]}}`)
					case "embeddings":
						if body["dimensions"] != float64(2) {
							t.Error("dimension lost", body)
						}
						if providerName == "Upstage" && body["model"] != "solar-embedding-1-large-query" {
							t.Error("query model not selected", body)
						}
						fmt.Fprint(w, `{"data":[{"index":1,"embedding":[1,0]},{"index":0,"embedding":[0,1]}]}`)
					case "v2/rerank":
						fmt.Fprint(w, `{"results":[{"index":1,"relevance_score":0.8},{"index":0,"relevance_score":0}]}`)
					case "v1/models":
						fmt.Fprint(w, `{"models":[{"name":"command-model"}]}`)
					case "models":
						fmt.Fprint(w, `{"data":[{"id":"chat-model"}]}`)
					case "model":
						fmt.Fprint(w, `{"items":[{"_id":"voice-id","title":"Voice"}]}`)
					case "wallet/self/api-credit":
						fmt.Fprint(w, `{"credit":0}`)
					default:
						t.Error("unexpected endpoint", path)
						http.NotFound(w, r)
					}
				}))
				address := server.Listener.Addr().String()
				t.Logf("resource-open httptest %s", address)
				t.Cleanup(func() {
					server.Close()
					connection, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
					if err == nil {
						connection.Close()
						t.Errorf("listener remains %s", address)
					}
					t.Logf("resource-closed-readback httptest %s", address)
				})
				extra, err := encodeProviderInstanceExtra("office", server.URL+"/instance/")
				if err != nil {
					t.Fatal(err)
				}
				driver, api, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", APIKey: instance, Extra: extra})
				if err != nil {
					t.Fatal(err)
				}
				if driver.Name() != strings.ToLower(providerName) {
					t.Fatal("factory fell back", driver.Name())
				}
				if providerName == "FishAudio" {
					names, err := driver.ListModels(api)
					if err != nil || !reflect.DeepEqual(names, []string{"Voice"}) {
						t.Fatal(names, err)
					}
					balance, err := driver.Balance(api)
					if err != nil || balance["credit"] != float64(0) {
						t.Fatal(balance, err)
					}
					if err := driver.CheckConnection(api); err != nil {
						t.Fatal(err)
					}
					name := "voice-id"
					chat := models.NewChatModel(driver, &name, api)
					if response, err := chat.ChatWithMessages([]models.Message{{Role: "user", Content: "q"}}, nil); err == nil || response != nil {
						t.Fatal("unsupported FishAudio chat succeeded")
					}
					if calls.Load() != 3 {
						t.Fatal("unexpected network call", calls.Load())
					}
				} else {
					name := "chat-model"
					chat := models.NewChatModel(driver, &name, api)
					messages := []models.Message{{Role: "user", Content: "q"}}
					if response, err := chat.ChatWithMessages(messages, nil); err != nil || response == nil || *response.Answer != "answer" {
						t.Fatal(response, err)
					}
					var output []string
					if err := chat.ChatStreamlyWithMessages(messages, nil, func(answer, reasoning *string) error {
						if answer != nil {
							output = append(output, *answer)
						}
						return nil
					}); err != nil || !reflect.DeepEqual(output, []string{"answer", "[DONE]"}) {
						t.Fatal(output, err)
					}
					if providerName != "StepFun" {
						embeddingName := "solar-embedding-1-large-passage"
						bound := models.NewEmbeddingModel(driver, &embeddingName, api)
						bound.EmbeddingConfig = &models.EmbeddingConfig{Query: true, Dimension: 2}
						data, err := bound.Embed([]string{"a", "b"})
						vectors, decodeErr := models.EmbeddingVectors(data, 2)
						if err != nil || decodeErr != nil || !reflect.DeepEqual(vectors, [][]float64{{0, 1}, {1, 0}}) {
							t.Fatal(data, err, decodeErr)
						}
					}
					if providerName == "CoHere" {
						name := "rerank-model"
						scores, err := models.NewRerankModel(driver, &name, api).Rerank("q", []string{"a", "b"})
						if err != nil || !reflect.DeepEqual(scores, []float64{0, 0.8}) {
							t.Fatal(scores, err)
						}
					}
					if providerName == "Baichuan" {
						if names, err := driver.ListModels(api); err == nil || names != nil {
							t.Fatal("unsupported discovery succeeded")
						}
						if err := driver.CheckConnection(api); err == nil {
							t.Fatal("unsupported connection succeeded")
						}
						if calls.Load() != 3 {
							t.Fatal(calls.Load())
						}
					} else {
						if err := driver.CheckConnection(api); err != nil {
							t.Fatal(err)
						}
					}
				}
				if provider.URL["default"] != originalURL || api.Context != nil || *api.APIKey != instance {
					t.Fatal("shared provider or caller configuration mutated")
				}
				t.Logf("%s/%s: %d authenticated real HTTP requests through catalog -> instanceModelDriver -> bound consumers", providerName, instance, calls.Load())
			})
		}
	}
}
