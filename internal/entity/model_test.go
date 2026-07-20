package entity

import (
	"path/filepath"
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
}
