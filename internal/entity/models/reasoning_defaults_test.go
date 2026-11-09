package models

import (
	"encoding/json"
	"fmt"
	"net/http"
	"reflect"
	"sync"
	"sync/atomic"
	"testing"
)

func TestReasoningDefaultsPreserveCallerConfigHTTP(t *testing.T) {
	for _, provider := range []string{"DeepSeek", "VolcEngine"} {
		for _, scenario := range []string{"nil", "empty", "thinking", "default", "disabled", "low"} {
			t.Run(provider+"/"+scenario, func(t *testing.T) {
				var config *ChatConfig
				if scenario != "nil" {
					config = &ChatConfig{}
				}
				if scenario != "nil" && scenario != "empty" {
					on := scenario != "disabled"
					config.Thinking = &on
				}
				if scenario == "default" || scenario == "low" {
					config.Effort = &scenario
				}
				before, err := json.Marshal(config)
				if err != nil {
					t.Fatal(err)
				}
				var calls atomic.Int32
				server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					calls.Add(1)
					var body map[string]any
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
						return
					}
					if r.URL.Path != "/instance/chat" || r.Header.Get("Authorization") != "Bearer fixture" {
						t.Error("instance endpoint or authentication changed")
					}
					var thinking any
					var effort any
					if config != nil && config.Thinking != nil {
						mode := "disabled"
						if *config.Thinking && !(provider == "DeepSeek" && scenario == "low") {
							mode = "enabled"
							effort = "high"
							if provider == "VolcEngine" {
								effort = "medium"
								if scenario == "low" {
									effort = "low"
								}
							}
						}
						thinking = map[string]any{"type": mode}
					}
					if !reflect.DeepEqual(body["thinking"], thinking) || body["reasoning_effort"] != effort {
						t.Errorf("thinking = %v, effort = %v; want %v, %v", body["thinking"], body["reasoning_effort"], thinking, effort)
					}
					if body["stream"] == true {
						fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
					} else {
						fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
					}
				}))
				driver, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL + "/instance"}, URLSuffix{Chat: "chat"})
				if err != nil {
					t.Fatal(err)
				}
				api := &APIConfig{APIKey: stringPtr("fixture")}
				name, question := "served", "question"
				var group sync.WaitGroup
				for range 3 {
					group.Go(func() {
						response, err := driver.ChatWithMessages(name, []Message{{Role: "user", Content: question}}, api, config)
						if err != nil || response == nil || response.Answer == nil || *response.Answer != "answer" {
							t.Error("sync response", response, err)
						}
						var frames []string
						err = driver.ChatStreamlyWithSender(name, []Message{{Role: "user", Content: question}}, api, config, func(content, _ *string) error {
							if content != nil {
								frames = append(frames, *content)
							}
							return nil
						})
						if err != nil || !reflect.DeepEqual(frames, []string{"answer", "[DONE]"}) {
							t.Error("stream response", frames, err)
						}
					})
				}
				group.Wait()
				after, err := json.Marshal(config)
				if err != nil || string(before) != string(after) || api.Context != nil || calls.Load() != 6 {
					t.Fatal("caller configuration or request count changed", string(before), string(after), calls.Load(), err)
				}
			})
		}
	}
}

func TestBoundReasoningDefaultsHTTP(t *testing.T) {
	for _, provider := range []string{"DeepSeek", "VolcEngine"} {
		t.Run(provider, func(t *testing.T) {
			server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body struct {
					Stream   bool              `json:"stream"`
					Effort   string            `json:"reasoning_effort"`
					Thinking map[string]string `json:"thinking"`
					Messages []Message         `json:"messages"`
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				want := "high"
				if provider == "VolcEngine" {
					want = "medium"
				}
				if body.Effort != want || body.Thinking["type"] != "enabled" || len(body.Messages) != 2 || body.Messages[0].Role != "system" {
					t.Error("bound defaults or history changed", body)
				}
				if body.Stream {
					fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
				} else {
					fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
				}
			}))
			driver, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
			if err != nil {
				t.Fatal(err)
			}
			name, on := "served", true
			bound := NewChatModel(driver, &name, &APIConfig{APIKey: stringPtr("fixture")})
			bound.ModelConfig.Thinking = &on
			history := []map[string]string{{"role": "user", "content": "question"}}
			if answer, err := bound.Chat("rules", history, nil); err != nil || answer != "answer" {
				t.Fatal(answer, err)
			}
			if err := bound.ChatStreamlyWithSender(chatHistory("rules", history), nil, func(*string, *string) error { return nil }); err != nil {
				t.Fatal(err)
			}
			if bound.ModelConfig.Effort != nil || bound.ModelConfig.Verbosity != nil || bound.ModelConfig.Thinking != &on || !on {
				t.Fatal("bound defaults mutated")
			}
		})
	}
}
