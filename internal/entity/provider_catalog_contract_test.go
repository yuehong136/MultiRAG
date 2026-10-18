package entity

import (
	"path/filepath"
	"testing"

	"multirag/internal/entity/models"
)

func TestOpenRouterAndVolcEngineCatalogContracts(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	router := manager.FindProvider("OpenRouter")
	if router == nil {
		t.Fatal("OpenRouter catalog not loaded")
	}
	if _, ok := router.ModelDriver.(*models.OpenRouterModel); !ok {
		t.Fatal("OpenRouter factory not registered")
	}
	if router.URLSuffix.Chat != "chat/completions" || router.URLSuffix.Embedding != "embeddings" || router.URLSuffix.Rerank != "rerank" || router.URLSuffix.Balance != "credits" || len(router.Models) != 3 {
		t.Fatal("OpenRouter catalog contract changed")
	}
	for _, model := range router.Models {
		if !model.ModelTypeMap["chat"] || model.Thinking == nil || !model.Thinking.DefaultValue || !model.Thinking.ClearThinking {
			t.Fatal("OpenRouter model metadata missing", model.Name)
		}
	}
	volc := manager.FindProvider("VolcEngine")
	model, err := manager.GetModelByName("VolcEngine", "doubao-embedding-vision-250615")
	if err != nil || volc.URLSuffix.Embedding != "embeddings/multimodal" || !model.ModelTypeMap["embedding"] || model.ModelTypeMap["chat"] || model.MaxTokens != 131072 {
		t.Fatal("VolcEngine embedding catalog mismatch", model, err)
	}
	chat, err := manager.GetModelByName("VolcEngine", "doubao-seed-2-0-pro-260215")
	if err != nil || !chat.ModelTypeMap["chat"] {
		t.Fatal("VolcEngine chat metadata lost", chat, err)
	}
}
