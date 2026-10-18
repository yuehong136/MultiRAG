package service

import (
	"encoding/json"
	"fmt"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"testing"
)

func TestHuggingFaceCatalogBoundCapabilitiesHTTP(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	provider := manager.FindProvider("HuggingFace")
	if provider == nil || provider.URLSuffix.Chat != "chat/completions" || provider.URLSuffix.Models != "models" || provider.URLSuffix.Embedding != "hf-inference/models" {
		t.Fatal("catalog endpoints missing", provider)
	}
	original := provider.URL["default"]
	for _, instance := range []string{"one", "two"} {
		t.Run(instance, func(t *testing.T) {
			t.Parallel()
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer "+instance {
					t.Error("key crossed instance")
				}
				switch r.URL.Path {
				case "/v1/chat/completions":
					var body map[string]any
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
					}
					if body["stream"] == true {
						fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
					} else {
						if body["temperature"] != float64(.6) {
							t.Error("chat default lost", body)
						}
						fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
					}
				case "/v1/models":
					fmt.Fprint(w, `{"data":[{"id":"org/m"}]}`)
				case "/hf-inference/models/org/m":
					fmt.Fprint(w, `[[0,1],[2,0]]`)
				default:
					t.Error(r.URL.Path)
					w.WriteHeader(404)
				}
			}))
			defer server.Close()
			extra, _ := encodeProviderInstanceExtra("office", server.URL+"/v1/")
			driver, api, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", APIKey: instance, Extra: extra})
			if err != nil {
				t.Fatal(err)
			}
			name := "org/m"
			messages := []models.Message{{Role: "user", Content: "q"}}
			response, err := driver.ChatWithMessages(name, api, messages, nil)
			if err != nil || response == nil || *response.Answer != "answer" {
				t.Fatal(response, err)
			}
			frames := []string{}
			err = driver.ChatStreamlyWithMessages(name, messages, api, nil, func(c, r *string) error {
				if c != nil {
					frames = append(frames, *c)
				}
				return nil
			})
			if err != nil || !reflect.DeepEqual(frames, []string{"answer", "[DONE]"}) {
				t.Fatal(frames, err)
			}
			names, err := driver.ListModels(api)
			if err != nil || !reflect.DeepEqual(names, []string{"org/m"}) {
				t.Fatal(names, err)
			}
			if err := driver.CheckConnection(api); err != nil {
				t.Fatal(err)
			}
			vectors, err := driver.Encode(&name, []string{"a", "b"}, api, nil)
			if err != nil || !reflect.DeepEqual(vectors, [][]float64{{0, 1}, {2, 0}}) {
				t.Fatal(vectors, err)
			}
			if provider.URL["default"] != original {
				t.Fatal("catalog mutated")
			}
		})
	}
}
