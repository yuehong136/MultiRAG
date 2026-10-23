package entity

import (
	"path/filepath"
	"strings"
	"testing"
)

func TestAddedProviderCatalogsAndFactory(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		provider, model, capability string
		count                       int
	}{
		{"CoHere", "embed-v4.0", "embedding", 4},
		{"FishAudio", "", "", 0},
		{"StepFun", "step-1v-32k", "vision", 11},
		{"Baichuan", "Baichuan-Text-Embedding", "embedding", 11},
		{"Upstage", "solar-embedding-1-large-query", "embedding", 6},
	} {
		t.Run(test.provider, func(t *testing.T) {
			provider := manager.FindProvider(test.provider)
			if provider == nil || provider.ModelDriver == nil || provider.ModelDriver.Name() != strings.ToLower(test.provider) || len(provider.Models) != test.count {
				t.Fatalf("catalog or factory missing: %#v", provider)
			}
			if test.model != "" {
				model, err := manager.GetModelByName(test.provider, test.model)
				if err != nil || model == nil || !model.ModelTypeMap[test.capability] {
					t.Fatal(model, err)
				}
			}
		})
	}
	cohere := manager.FindProvider("CoHere")
	if cohere.URLSuffix.Embedding != "v2/embed" || cohere.URLSuffix.Chat != "v2/chat" || cohere.URLSuffix.Rerank != "v2/rerank" || cohere.URLSuffix.Models != "v1/models" {
		t.Fatal("Cohere V2 endpoint wiring", cohere.URLSuffix)
	}
	fish := manager.FindProvider("FishAudio")
	if fish.URLSuffix.Balance != "wallet/self/api-credit" {
		t.Fatal("FishAudio balance endpoint", fish.URLSuffix)
	}
	volc := manager.FindProvider("VolcEngine")
	if volc.URLSuffix.Models != "models" {
		t.Fatal("active VolcEngine discovery endpoint lost")
	}
	for _, name := range []string{"doubao-embedding-vision-250615", "doubao-embedding-vision-251215"} {
		model, err := manager.GetModelByName("VolcEngine", name)
		if err != nil || model == nil || !model.ModelTypeMap["embedding"] {
			t.Fatal(name, model, err)
		}
	}
}
