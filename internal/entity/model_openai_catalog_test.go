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
	for _, name := range []string{"gpt-5.5", "gpt-5.4"} {
		model, err := manager.GetModelByName("OpenAI", name)
		if err != nil || model == nil || model.MaxTokens != 400000 || !model.ModelTypeMap["chat"] || !model.ModelTypeMap["vision"] {
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
}
