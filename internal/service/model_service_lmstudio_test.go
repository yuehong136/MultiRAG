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

func TestLMStudioConfiguredInstanceHTTPIsolation(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	provider := manager.FindProvider("LM-Studio")
	if provider == nil || provider.ModelDriver.Name() != "LM-Studio" || provider.Class != "local" || provider.URLSuffix.Chat != "chat/completions" || provider.URLSuffix.Models != "models" || len(provider.Models) != 0 || len(provider.URL) != 0 {
		t.Fatal("LM Studio config/driver not wired")
	}
	off := false
	for _, test := range []struct{ name, key, region string }{{"one", "", "default"}, {"two", "fixture-key", "office"}} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				auth := ""
				if test.key != "" {
					auth = "Bearer " + test.key
				}
				if r.Header.Get("Authorization") != auth {
					t.Error("instance auth leaked")
				}
				switch r.URL.Path {
				case "/v1/models":
					if r.Method != "GET" || r.ContentLength > 0 {
						t.Error("model discovery request")
					}
					fmt.Fprintf(w, `{"data":[{"id":%q}]}`, test.name)
				case "/v1/chat/completions":
					var body struct {
						Model    string
						Stream   bool
						Messages []models.Message
						Thinking struct{ Type string }
					}
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
						return
					}
					if r.Method != "POST" || body.Model != test.name || len(body.Messages) != 3 || body.Messages[0].Role != "system" || body.Messages[1].Role != "assistant" || body.Thinking.Type != "disabled" {
						t.Error("bound history/config lost")
					}
					if body.Stream {
						fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":%q,\"reasoning_content\":\"reason\"}}]}\n\ndata: [DONE]\n\n", test.name)
					} else {
						fmt.Fprintf(w, `{"choices":[{"message":{"content":%q,"reasoning_content":"reason"}}]}`, test.name)
					}
				default:
					t.Errorf("wrong instance endpoint %s", r.URL.Path)
					http.NotFound(w, r)
				}
			}))
			defer server.Close()
			raw, err := encodeProviderInstanceExtra(test.region, server.URL+"/v1/")
			if err != nil {
				t.Fatal(err)
			}
			driver, api, err := instanceModelDriver(provider, &entity.TenantModelInstance{InstanceName: test.name, Status: "active", APIKey: test.key, Extra: raw})
			if err != nil || driver == provider.ModelDriver || *api.Region != test.region {
				t.Fatal(driver, api, err)
			}
			bound := models.NewChatModel(driver, &test.name, api)
			bound.ModelConfig.Thinking = &off
			history := []models.Message{{Role: "system", Content: "rules"}, {Role: "assistant", Content: "previous"}, {Role: "user", Content: "q"}}
			response, err := bound.ChatWithMessages(history, nil)
			if err != nil || response == nil || *response.Answer != test.name || *response.ReasoningContent != "reason" {
				t.Fatal(response, err)
			}
			var frames []string
			err = bound.ChatStreamlyWithMessages(history, nil, func(c, r *string) error {
				if r != nil {
					frames = append(frames, *r)
				}
				if c != nil {
					frames = append(frames, *c)
				}
				return nil
			})
			if err != nil || !reflect.DeepEqual(frames, []string{"reason", test.name, "[DONE]"}) {
				t.Fatal(frames, err)
			}
			names, err := driver.ListModels(api)
			if err != nil || !reflect.DeepEqual(names, []string{test.name}) || calls.Load() != 3 {
				t.Fatal(names, err, calls.Load())
			}
			if len(provider.URL) != 0 || len(provider.ModelDriver.(*models.LMStudioModel).BaseURL) != 0 {
				t.Fatal("shared catalog mutated")
			}
			t.Logf("instance %s: isolated POST chat/SSE and GET models; region=%s, optional auth verified; 3 HTTP calls", test.name, test.region)
		})
	}
	if _, _, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", Extra: "{}"}); err == nil {
		t.Fatal("missing instance URL accepted")
	}
	if _, _, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "inactive", Extra: "{}"}); err == nil {
		t.Fatal("disabled instance accepted")
	}
}
