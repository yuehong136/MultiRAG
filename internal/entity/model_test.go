package entity

import (
	"path/filepath"
	"slices"
	"testing"
)

func TestProviderModelsKeepInitializedTypeMaps(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatalf("NewProviderManager() error = %v", err)
	}

	model, err := manager.GetModelByName("zhipu-ai", "glm-4.6v-flash")
	if err != nil {
		t.Fatalf("GetModelByName() error = %v", err)
	}
	if !model.ModelTypeMap["chat"] || !model.ModelTypeMap["image2text"] {
		t.Fatalf("ModelTypeMap = %#v, want chat and image2text", model.ModelTypeMap)
	}

	provider := manager.FindProvider("zhipu-ai")
	if provider == nil {
		t.Fatal("FindProvider() returned nil")
	}
	if got := provider.URL["default"]; got != "https://open.bigmodel.cn/api/paas/v4" {
		t.Fatalf("default provider URL = %q", got)
	}
}

func TestProviderModelsResolveThinkingFeatures(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatalf("NewProviderManager() error = %v", err)
	}

	thinkingModel, err := manager.GetModelByName("zhipu-ai", "glm-4.5-air")
	if err != nil {
		t.Fatalf("GetModelByName() error = %v", err)
	}
	if thinkingModel.Thinking == nil || !thinkingModel.Thinking.DefaultValue || !thinkingModel.Thinking.ClearContent {
		t.Fatalf("Thinking = %#v", thinkingModel.Thinking)
	}

	plainModel, err := manager.GetModelByName("zhipu-ai", "glm-4-plus")
	if err != nil {
		t.Fatalf("GetModelByName() error = %v", err)
	}
	if plainModel.Thinking != nil {
		t.Fatalf("Thinking = %#v, want nil", plainModel.Thinking)
	}

	models, err := manager.ListModels("zhipu-ai")
	if err != nil {
		t.Fatalf("ListModels() error = %v", err)
	}
	for _, model := range models {
		if model["name"] == "glm-4.7" {
			features, ok := model["features"].([]string)
			if !ok || !slices.Contains(features, "thinking") {
				t.Fatalf("glm-4.7 features = %#v", model["features"])
			}
			return
		}
	}
	t.Fatal("glm-4.7 not found")
}

// SHOW BALANCE 依赖 provider 配置里的 balance suffix：Moonshot 的驱动早已实现
// Balance()，但在补上这份配置之前 FindProvider 返回 nil，命令直接 404。
func TestMoonshotProviderSupportsBalanceLookup(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatalf("NewProviderManager() error = %v", err)
	}

	provider := manager.FindProvider("moonshot")
	if provider == nil {
		t.Fatal("FindProvider(\"moonshot\") returned nil")
	}
	if got := provider.URL["default"]; got != "https://api.moonshot.cn/v1" {
		t.Fatalf("default provider URL = %q", got)
	}
	if got := provider.URLSuffix.Balance; got != "users/me/balance" {
		t.Fatalf("balance suffix = %q, want users/me/balance", got)
	}
	if got := provider.URLSuffix.Models; got != "models" {
		t.Fatalf("models suffix = %q, want models", got)
	}

	model, err := manager.GetModelByName("moonshot", "kimi-k2.6")
	if err != nil {
		t.Fatalf("GetModelByName() error = %v", err)
	}
	if model.Thinking == nil || !model.Thinking.ClearContent {
		t.Fatalf("kimi-k2.6 thinking = %#v, want clear_thinking enabled", model.Thinking)
	}
}

func TestDeepSeekProviderIsConfigured(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatalf("NewProviderManager() error = %v", err)
	}

	provider := manager.FindProvider("deepseek")
	if provider == nil {
		t.Fatal("FindProvider(\"deepseek\") returned nil")
	}
	if got := provider.URL["default"]; got != "https://api.deepseek.com" {
		t.Fatalf("default provider URL = %q", got)
	}

	model, err := manager.GetModelByName("deepseek", "deepseek-reasoner")
	if err != nil {
		t.Fatalf("GetModelByName() error = %v", err)
	}
	if !model.ModelTypeMap["chat"] {
		t.Fatalf("ModelTypeMap = %#v, want chat", model.ModelTypeMap)
	}
}
