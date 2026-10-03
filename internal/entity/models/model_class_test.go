package models

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
)

type modelClassChatDriver struct {
	ModelDriver
	class *string
}

func (d *modelClassChatDriver) ChatWithMessages(_ string, _ *string, _ []Message, config *ChatConfig) (string, error) {
	d.class = config.ModelClass
	return "answer", nil
}

func TestBoundChatPreservesModelClass(t *testing.T) {
	driver := &modelClassChatDriver{}
	name, key, modelClass := "alias", "fixture-key", "qwen3"
	bound := NewChatModel(driver, &name, &APIConfig{APIKey: &key})
	bound.ModelConfig.ModelClass = &modelClass
	answer, err := bound.Chat("system", []map[string]string{{"role": "user", "content": "question"}}, map[string]interface{}{
		"thinking": false, "model_class": "gpt", "model_type": "embedding",
	})
	if err != nil || answer != "answer" {
		t.Fatalf("bound chat = %q, %v", answer, err)
	}
	assertStringPointer(t, driver.class, &modelClass)
}

func TestModelClassFromNamespacedName(t *testing.T) {
	for name, want := range map[string]string{
		"Qwen/Qwen3-8B": "qwen3", "qwen/qwen3.5-4b": "qwen3.5",
		"org/GLM-4.7": "glm", "vendor/plain": "plain", "plain": "plain",
	} {
		if got := ModelClassFromName(name); got != want {
			t.Fatalf("%s class = %s, want %s", name, got, want)
		}
	}
}

func TestChatConsumersUseModelClass(t *testing.T) {
	for _, provider := range []string{"Gitee", "SiliconFlow"} {
		for _, test := range []struct {
			name, model, class, async, path string
			split                           bool
		}{
			{name: "namespaced inferred family", model: "Qwen/Qwen3-8B", path: "/chat/completions", split: true},
			{name: "explicit family", model: "alias", class: "Qwen3", path: "/chat/completions", split: true},
			{name: "explicit other family", model: "qwen3-8b", class: "gpt", path: "/chat/completions"},
			{name: "missing async suffix", model: "GLM-4.7", path: "/chat/completions"},
			{name: "configured async suffix", model: "alias", class: "glm", async: "async/chat", path: "/async/chat"},
		} {
			t.Run(provider+"/"+test.name, func(t *testing.T) {
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.Method != http.MethodPost || r.URL.Path != test.path || r.Header.Get("Authorization") != "Bearer fixture-key" {
						t.Errorf("request = %s %s, wrong endpoint or credential", r.Method, r.URL.Path)
					}
					var body struct{ Model string }
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Model != test.model {
						t.Errorf("model ID changed: %q, %v", body.Model, err)
					}
					fmt.Fprint(w, `{"choices":[{"message":{"content":"<think>reason</think>answer"}}]}`)
				}))
				defer server.Close()
				driver, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Chat: "chat/completions", AsyncChat: test.async})
				if err != nil {
					t.Fatal(err)
				}
				config := &ChatConfig{}
				if test.class != "" {
					config.ModelClass = &test.class
				}
				key, message := "fixture-key", "question"
				response, err := driver.Chat(&test.model, &message, &APIConfig{APIKey: &key}, config)
				if err != nil {
					t.Fatal(err)
				}
				if test.split {
					assertStringPointer(t, response.Answer, stringPtr("answer"))
					assertStringPointer(t, response.ReasoningContent, stringPtr("reason"))
				} else {
					assertStringPointer(t, response.Answer, stringPtr("<think>reason</think>answer"))
					assertStringPointer(t, response.ReasoningContent, nil)
				}
			})
		}
	}
}
