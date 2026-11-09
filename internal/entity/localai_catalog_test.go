package entity

import (
	modelModule "multirag/internal/entity/models"

	"path/filepath"
	"testing"
)

func TestLocalAICatalogAndFactory(t *testing.T) {
	manager, err := modelModule.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	_, err = manager.GetProviderByName("LocalAI")
	if err != nil {
		t.Fatal(err)
	}
	var provider modelModule.Provider
	for _, candidate := range manager.Providers {
		if candidate.Name == "LocalAI" {
			provider = candidate
			break
		}
	}
	if provider.Name != "LocalAI" || provider.ModelDriver.Name() != "LocalAI" || provider.URL["default"] != "" {
		t.Fatal("canonical/keyless catalog contract")
	}
}
