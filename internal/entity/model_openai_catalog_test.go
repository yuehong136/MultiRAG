package entity

import (
	"path/filepath"
	"testing"
)

func TestOpenAILatestCatalogModels(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	listed, err := manager.ListModels("OpenAI")
	if err != nil {
		t.Fatal(err)
	}
	for name, contextLength := range map[string]int{"gpt-6.1-sol": 1050000, "gpt-6-luna": 1050000, "gpt-5.6-sol": 1050000, "gpt-5.5": 1050000, "gpt-5.4": 1050000, "gpt-5.4-mini": 400000} {
		model, err := manager.GetModelByName("OpenAI", name)
		if err != nil || model == nil || model.MaxTokens != contextLength || !model.ModelTypeMap["chat"] || !model.ModelTypeMap["vision"] {
			t.Fatal("OpenAI model metadata missing", name, model, err)
		}
		count := 0
		for _, row := range listed {
			if row["name"] == name {
				count++
			}
		}
		if count != 1 {
			t.Fatalf("OpenAI catalog must list %s once; got %d", name, count)
		}
	}
	for _, name := range []string{"gpt-3.5-turbo", "gpt-4.1-nano", "gpt-4.5-preview", "gpt-4-32k", "gpt-5.2-pro", "gpt-5.4-nano"} {
		if model, err := manager.GetModelByName("OpenAI", name); err == nil || model != nil {
			t.Errorf("retired or incompatible model remains in built-in catalog: %s", name)
		}
	}
}
