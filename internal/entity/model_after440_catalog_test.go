package entity

import (
	"multirag/internal/entity/models"
	"path/filepath"
	"testing"
)

func TestAfter440ProviderCatalogCapabilities(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	for name, count := range map[string]int{"Avian": 6, "Astraflow": 18, "Perplexity": 6} {
		p := manager.FindProvider(name)
		if p == nil || len(p.Models) != count || p.ModelDriver.Name() == "dummy" {
			t.Fatal(name, p)
		}
	}
	for _, test := range []struct {
		provider, name, capability string
		tokens                     int
	}{
		{"Replicate", "replicate/all-mpnet-base-v2:b6b7585c9640cd7a9572c6e129c9549d79c9c31f0d3fdce7baac7c67ca38f305", "embedding", 384},
		{"Perplexity", "pplx-embed-v1-0.6b", "embedding", 32000},
		{"Mistral", "magistral-medium-latest", "chat", 40000},
		{"Mistral", "magistral-small-latest", "chat", 40000},
	} {
		model, err := manager.GetModelByName(test.provider, test.name)
		if err != nil || model.MaxTokens != test.tokens || !model.ModelTypeMap[test.capability] {
			t.Fatal(test, model, err)
		}
	}
	p := manager.FindProvider("Xinference")
	if p.URLSuffix.ASR != "v1/audio/transcriptions" || p.URLSuffix.TTS != "v1/audio/speech" {
		t.Fatal(p)
	}
	if _, ok := p.ModelDriver.(models.ASRDriver); !ok {
		t.Fatal("missing built-in ASR")
	}
	if _, ok := p.ModelDriver.(models.TTSDriver); !ok {
		t.Fatal("missing built-in TTS")
	}
	if _, ok := p.ModelDriver.(models.TTSStreamDriver); ok {
		t.Fatal("unsupported stream advertised")
	}
	if manager.FindProvider("Novita").URLSuffix.Balance != "openapi/v1/billing/balance/detail" {
		t.Fatal("balance suffix missing")
	}
}
