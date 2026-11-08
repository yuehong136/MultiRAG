package entity

import (
	"path/filepath"
	"testing"
)

func TestAfter22CatalogCanonicalAndCapabilities(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	if err := manager.LoadAllModels(filepath.Join("..", "..", "configs", "all_models.json")); err != nil {
		t.Fatal(err)
	}
	if got := len(manager.ListAllModels()); got != 2042 {
		t.Fatal("incomplete catalog", got)
	}
	for _, name := range []string{"gemini-2.5-pro", "meta-llama/llama-2-7b", "amazon.titan-embed-text-v1", "azure/gpt-4o"} {
		row, err := manager.GetModelByNameOrAlias(name)
		if err != nil || row == nil {
			t.Fatal(name, err)
		}
	}
	row, err := manager.GetModelByName("Gitee", "jina-clip-v2")
	if err != nil || !row.ModelTypeMap["embedding"] {
		t.Fatal("Gitee clip metadata absent", row, err)
	}
}
