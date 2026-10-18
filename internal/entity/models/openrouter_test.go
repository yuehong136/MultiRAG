package models

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
)

func TestOpenRouterChatHTTP(t *testing.T) {
	for _, instance := range []string{"qwen_model", "glm_model"} {
		t.Run(instance, func(t *testing.T) {
			t.Parallel()
			key := "key-" + instance
			history := []Message{{Role: "system", Content: "rules"}, {Role: "assistant", Content: "previous", ReasoningContent: stringPtr("thought")}, {Role: "user", Content: []any{map[string]any{"type": "text", "text": "question"}}}}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer "+key {
					t.Error("credentials crossed instances")
				}
				if r.URL.Path == "/v1/models" {
					if r.Method != http.MethodGet || r.ContentLength > 0 {
						t.Error("invalid discovery request")
					}
					fmt.Fprintf(w, `{"data":[{"id":%q}]}`, instance)
					return
				}
				if r.URL.Path != "/v1/chat/completions" || r.Method != http.MethodPost {
					t.Error("wrong chat endpoint")
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				messages, _ := body["messages"].([]any)
				if len(messages) != 3 || messages[1].(map[string]any)["reasoning"] != "thought" || body["model"] != instance || body["temperature"] != float64(0) || body["max_tokens"] != float64(12) {
					t.Error("history/options lost", body)
				}
				if !reflect.DeepEqual(body["reasoning"], map[string]any{"enabled": false}) || body["thinking"] != nil || body["do_sample"] != nil {
					t.Error("wrong reasoning controls", body)
				}
				if body["stream"] == true {
					fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"reasoning\":\"reason\",\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
				} else {
					fmt.Fprint(w, `{"choices":[{"message":{"content":"answer","reasoning":"reason"}}]}`)
				}
			}))
			defer server.Close()
			urls := map[string]string{"default": server.URL + "/v1/"}
			driver, err := NewModelFactory().CreateModelDriver("OpenRouter", urls, URLSuffix{Chat: "/chat/completions", Models: "models"})
			if err != nil || driver.Name() != "openrouter" {
				t.Fatal(driver, err)
			}
			urls["default"] = "http://invalid.invalid"
			api := &APIConfig{APIKey: &key, Region: stringPtr("")}
			on, off, zero, max := true, false, 0.0, 12
			config := &ChatConfig{Stream: &on, Thinking: &off, Temperature: &zero, MaxTokens: &max}
			response, err := driver.ChatWithMessages(instance, api, history, config)
			if err != nil || response == nil || *response.Answer != "answer" || *response.ReasoningContent != "reason" {
				t.Fatal(response, err)
			}
			config.Stream = &off
			var frames []string
			err = driver.ChatStreamlyWithMessages(instance, history, api, config, func(content, reasoning *string) error {
				if reasoning != nil {
					frames = append(frames, *reasoning)
				}
				if content != nil {
					frames = append(frames, *content)
				}
				return nil
			})
			if err != nil || !reflect.DeepEqual(frames, []string{"reason", "answer", "[DONE]"}) {
				t.Fatal(frames, err)
			}
			if names, err := driver.ListModels(api); err != nil || !reflect.DeepEqual(names, []string{instance}) {
				t.Fatal(names, err)
			}
		})
	}
}

func TestOpenRouterReasoningBody(t *testing.T) {
	for _, test := range []struct {
		config *ChatConfig
		want   any
	}{
		{nil, nil},
		{&ChatConfig{Thinking: minimaxPtr(true)}, map[string]any{"enabled": true}},
		{&ChatConfig{Effort: stringPtr("high")}, map[string]any{"effort": "high"}},
		{&ChatConfig{Thinking: minimaxPtr(false), Effort: stringPtr("high")}, map[string]any{"enabled": false}},
	} {
		for _, stream := range []bool{false, true} {
			body, err := openRouterChatBody("m", []Message{{Role: "user", Content: "q"}}, test.config, stream)
			if err != nil || !reflect.DeepEqual(body["reasoning"], test.want) {
				t.Fatal(body, err)
			}
		}
	}
}

func TestOpenRouterFailuresHTTP(t *testing.T) { testHostedChatFailures(t, "OpenRouter", false) }
func TestOpenRouterCancelHTTP(t *testing.T)   { testHostedCancellation(t, "OpenRouter", false) }

func TestOpenRouterDiscoveryFailures(t *testing.T) {
	for _, body := range []string{`{}`, `{"data":null}`, `{"data":[{}]}`, `{"data":[{"id":123}]}`, `{"error":"private-body","data":[]}`} {
		t.Run(body, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
			defer server.Close()
			driver := NewOpenRouterModel(map[string]string{"default": server.URL}, URLSuffix{Models: "models"})
			if _, err := driver.ListModels(&APIConfig{APIKey: stringPtr("key")}); err == nil || strings.Contains(err.Error(), "private-body") {
				t.Fatal(err)
			}
		})
	}
}

func TestProviderEmptyRegionChatHTTP(t *testing.T) {
	for _, provider := range []string{"Aliyun", "DeepSeek", "Gitee", "SiliconFlow", "Moonshot", "NVIDIA", "OpenAI", "OpenRouter", "VolcEngine", "XAI", "ZHIPU-AI", "LM-Studio", "vllm"} {
		t.Run(provider, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/chat/completions" {
					t.Error("empty region did not select default", r.URL.Path)
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if body["stream"] == true {
					fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
				} else {
					fmt.Fprint(w, `{"choices":[{"message":{"content":"answer","reasoning_content":"reason"}}]}`)
				}
			}))
			defer server.Close()
			driver, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Chat: "chat/completions"})
			api := &APIConfig{APIKey: stringPtr("key"), Region: stringPtr("")}
			history := []Message{{Role: "user", Content: "q"}}
			response, err := driver.ChatWithMessages("model", api, history, nil)
			if err != nil || response == nil || *response.Answer != "answer" {
				t.Fatal(response, err)
			}
			if err := driver.ChatStreamlyWithMessages("model", history, api, nil, func(*string, *string) error { return nil }); err != nil {
				t.Fatal(err)
			}
		})
	}
}
