package service

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

func TestNVIDIAConfiguredInstancesHTTP(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	provider := manager.FindProvider("NVIDIA")
	if provider == nil || provider.ModelDriver.Name() != "nvidia" || provider.URL["default"] != "https://integrate.api.nvidia.com/v1" {
		t.Fatal("NVIDIA config/factory")
	}
	// All target catalog models remain available as provider metadata, not driver capability promises.
	if len(provider.Models) != 64 {
		t.Fatalf("catalog models=%d", len(provider.Models))
	}
	original := provider.URL["default"]
	for _, instance := range []string{"one", "two"} {
		t.Run(instance, func(t *testing.T) {
			t.Parallel()
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/custom/v1/chat/completions" || r.Header.Get("Authorization") != "Bearer "+instance {
					t.Error("instance binding lost")
				}
				var body struct {
					Stream   bool
					Thinking struct{ Type string }
					Messages []models.Message
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if body.Thinking.Type != "disabled" || len(body.Messages) != 3 || body.Messages[0].Role != "system" || body.Messages[1].Role != "assistant" {
					t.Error("bound history/thinking defaults")
				}
				if body.Stream {
					fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":%q}}]}\n\ndata: [DONE]\n\n", instance)
				} else {
					fmt.Fprintf(w, `{"choices":[{"message":{"content":%q}}]}`, instance)
				}
			}))
			defer server.Close()
			extra, _ := encodeProviderInstanceExtra("office", server.URL+"/custom/v1/")
			driver, api, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", APIKey: instance, Extra: extra})
			if err != nil {
				t.Fatal(err)
			}
			model := models.NewChatModel(driver, &instance, api)
			off := false
			model.ModelConfig.Thinking = &off
			history := []models.Message{{Role: "system", Content: "rules"}, {Role: "assistant", Content: "previous"}, {Role: "user", Content: "q"}}
			answer, err := model.ChatWithMessages(history, nil)
			if err != nil || *answer.Answer != instance {
				t.Fatal(answer, err)
			}
			done, content := 0, ""
			err = model.ChatStreamlyWithMessages(history, nil, func(c, r *string) error {
				if c != nil {
					if *c == "[DONE]" {
						done++
					} else {
						content += *c
					}
				}
				return nil
			})
			if err != nil || done != 1 || content != instance {
				t.Fatal(content, done, err)
			}
			if provider.URL["default"] != original || provider.ModelDriver.(*models.NVIDIAModel).BaseURL["default"] != original {
				t.Fatal("catalog mutated")
			}
			t.Log("bound NVIDIA ordinary/SSE calls made real controlled HTTP requests and preserved the catalog")
		})
	}
}
