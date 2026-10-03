package entity

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestProviderModelClassPrecedence(t *testing.T) {
	directory := t.TempDir()
	config := `{"name":"Gitee","class":"gpt","url":{"default":"https://example.invalid"},"models":[{"name":"mixed","class":"Qwen3","model_types":["chat"]},{"name":"default","model_types":["embedding"]}]}`
	if err := os.WriteFile(filepath.Join(directory, "provider.json"), []byte(config), 0600); err != nil {
		t.Fatal(err)
	}
	manager, err := NewProviderManager(directory)
	if err != nil {
		t.Fatal(err)
	}
	for name, want := range map[string]string{"mixed": "qwen3", "default": "gpt"} {
		model, err := manager.GetModelByName("Gitee", name)
		if err != nil || model.Class == nil || *model.Class != want {
			t.Fatalf("%s class = %v, %v; want %s", name, model, err, want)
		}
	}
	if response := manager.SearchByType("qwen3"); response.Code != 404 {
		t.Fatalf("model family used as capability: %#v", response)
	}
	if response := manager.SearchByType("embedding"); response.Code != 0 || len(response.Data) != 1 || response.Data[0]["name"] != "default" {
		t.Fatalf("embedding capability changed: %#v", response)
	}
}

func TestConfiguredModelClassesKeepCapabilities(t *testing.T) {
	directory := filepath.Join("..", "..", "configs", "models")
	manager, err := NewProviderManager(directory)
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct{ provider, model, class string }{
		{"Aliyun", "qwen-flash", "qwen"},
		{"DeepSeek", "deepseek-v4-pro", "deepseek"},
		{"Gitee", "qwen3-8b", "qwen3"},
		{"Google", "gemini-2.5-flash", "gemini"},
		{"MiniMax", "minimax-m2.7", "minimax"},
		{"Moonshot", "kimi-k2.6", "kimi"},
		{"OpenAI", "gpt-5.2-pro", "gpt"},
		{"SiliconFlow", "qwen/qwen3-8b", "qwen3"},
		{"VolcEngine", "doubao-seed-2-0-pro-260215", "volcengine"},
		{"xAI", "grok-4", "grok"},
		{"ZHIPU-AI", "glm-5", "glm"},
	} {
		t.Run(test.provider, func(t *testing.T) {
			model, err := manager.GetModelByName(test.provider, test.model)
			if err != nil || model.Class == nil || *model.Class != test.class || !model.ModelTypeMap["chat"] {
				t.Fatalf("model family/capability = %#v, %v", model, err)
			}
			if _, _, err := manager.GetModelUrl(test.provider, test.model, test.class); err == nil {
				t.Fatal("model family accepted as endpoint capability")
			}
		})
	}
	if provider := manager.FindProvider("Google"); provider.Class != "gemini" {
		t.Fatalf("Google provider class = %q", provider.Class)
	}
	files, err := filepath.Glob(filepath.Join(directory, "*.json"))
	if err != nil {
		t.Fatal(err)
	}
	for _, file := range files {
		data, err := os.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		var provider map[string]json.RawMessage
		if err := json.Unmarshal(data, &provider); err != nil {
			t.Fatal(err)
		}
		if provider["series"] != nil || provider["type"] != nil {
			t.Fatalf("%s uses a retired family key", file)
		}
	}
}
