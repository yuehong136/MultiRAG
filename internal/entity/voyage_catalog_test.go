package entity

import (
	"path/filepath"
	"testing"
)

func TestVoyageCatalogAndFactory(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	_, err = manager.GetProviderByName("Voyage AI")
	if err != nil {
		t.Fatal(err)
	}
	var p Provider
	for _, candidate := range manager.Providers {
		if candidate.Name == "Voyage AI" {
			p = candidate
			break
		}
	}
	if p.Name != "Voyage AI" || p.ModelDriver.Name() != "Voyage AI" || len(p.Models) != 8 {
		t.Fatal("Voyage catalog/canonical name")
	}
}
