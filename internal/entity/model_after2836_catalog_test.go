package entity

import (
	"path/filepath"
	"testing"
)

func TestAfter2836ProviderCatalogCapabilities(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	for name, wantDriver := range map[string]string{"n1n": "n1n", "GPUStack": "gpustack", "Azure-OpenAI": "azure-openai"} {
		provider := manager.FindProvider(name)
		if provider == nil || provider.ModelDriver.Name() != wantDriver {
			t.Fatal("driver factory missing", name, provider)
		}
		if name != "n1n" && (provider.URL["default"] != "" || len(provider.Models) != 0) {
			t.Fatal("tenant deployment catalog has global endpoint or fake models", provider)
		}
	}
	for _, test := range []struct {
		provider, name, capability string
		tokens                     int
	}{
		{"TogetherAI", "intfloat/multilingual-e5-large-instruct", "embedding", 514},
		{"TogetherAI", "BAAI/bge-large-en-v1.5", "embedding", 512},
		{"TogetherAI", "BAAI/bge-base-en-v1.5", "embedding", 512},
		{"Novita", "baai/bge-reranker-v2-m3", "rerank", 1024},
		{"n1n", "gpt-4o-mini", "image2text", 128000},
		{"n1n", "text-embedding-3-small", "embedding", 8191},
		{"n1n", "BAAI/bge-reranker-v2-m3", "rerank", 8192},
	} {
		model, err := manager.GetModelByName(test.provider, test.name)
		if err != nil || model.MaxTokens != test.tokens || !model.ModelTypeMap[test.capability] {
			t.Fatal(test, model, err)
		}
	}
	if provider := manager.FindProvider("Xinference"); provider.URLSuffix.Embedding != "v1/embeddings" || provider.URLSuffix.Rerank != "v1/rerank" {
		t.Fatal("Xinference capability endpoints missing", provider)
	}
}
