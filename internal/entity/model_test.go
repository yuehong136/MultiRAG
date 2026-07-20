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
