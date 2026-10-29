package entity

import (
	"path/filepath"
	"testing"

	"multirag/internal/entity/models"
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
	if p.Name != "Voyage AI" || p.ModelDriver == nil || p.ModelDriver.Name() != "Voyage AI" {
		t.Fatal("Voyage catalog/canonical name")
	}
	driver, ok := p.ModelDriver.(*models.VoyageModel)
	if !ok {
		t.Fatalf("Voyage factory returned %T", p.ModelDriver)
	}
	if driver.BaseURL["default"] != "https://api.voyageai.com" || driver.URLSuffix.Embedding != "v1/embeddings" || driver.URLSuffix.Rerank != "v1/rerank" {
		t.Fatal("Voyage driver endpoint configuration changed")
	}
	seen := make(map[string]bool, len(p.Models))
	for _, model := range p.Models {
		if model == nil || model.Name == "" {
			t.Fatal("Voyage catalog contains an empty model")
		}
		if seen[model.Name] {
			t.Fatalf("duplicate Voyage model %q", model.Name)
		}
		seen[model.Name] = true
	}
	// Retain the original eight models and the five newer additions. Verify
	// their contracts rather than treating the old inventory size as a limit.
	for _, test := range []struct {
		name      string
		modelType string
		maxTokens int
	}{
		{"voyage-4-large", "embedding", 32000},
		{"voyage-4", "embedding", 32000},
		{"voyage-4-lite", "embedding", 32000},
		{"voyage-3.5", "embedding", 327680},
		{"voyage-3.5-lite", "embedding", 1048576},
		{"voyage-3-large", "embedding", 122880},
		{"voyage-code-3", "embedding", 122880},
		{"voyage-law-2", "embedding", 122880},
		{"voyage-finance-2", "embedding", 122880},
		{"rerank-2.5", "rerank", 32000},
		{"rerank-2.5-lite", "rerank", 32000},
		{"rerank-2", "rerank", 4000},
		{"rerank-2-lite", "rerank", 2000},
	} {
		requireCatalogModels(t, manager, "Voyage AI", []catalogModelContract{{name: test.name, modelTypes: []string{test.modelType}}})
		model, err := manager.GetModelByName("voyage ai", test.name)
		if err != nil || model == nil || model.Class == nil || *model.Class != "voyage" || model.MaxTokens != test.maxTokens {
			t.Fatalf("%s metadata = %#v, %v", test.name, model, err)
		}
	}
}
