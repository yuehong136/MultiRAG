package entity

import (
	"path/filepath"
	"testing"

	"multirag/internal/entity/models"
)

func TestBaiduCatalogRegistration(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	provider := manager.FindProvider("Baidu")
	if provider == nil || provider.ModelDriver.Name() != "baidu" || len(provider.Models) != 8 || provider.URLSuffix.Chat != "chat/completions" || provider.URLSuffix.Models != "models" || provider.URLSuffix.Embedding != "embeddings" || provider.URLSuffix.Rerank != "rerank" {
		t.Fatal("Baidu catalog/factory/endpoint wiring", provider)
	}
	if _, ok := provider.ModelDriver.(*models.BaiduModel); !ok {
		t.Fatal("Baidu uses a fallback driver")
	}
	for name, capability := range map[string]string{"deepseek-v3.2": "chat", "ernie-5.0": "vision", "embedding-v1": "embedding", "qwen3-reranker-4b": "rerank"} {
		model, err := manager.GetModelByName("baidu", name)
		if err != nil || model == nil || !model.ModelTypeMap[capability] {
			t.Fatal(name, model, err)
		}
	}
	for _, name := range []string{"deepseek-v4-flash", "deepseek-v4-pro"} {
		model, err := manager.GetModelByName("Baidu", name)
		if err != nil || model.Thinking == nil || !model.Thinking.DefaultValue || !model.Thinking.ClearThinking {
			t.Fatal(name, model, err)
		}
	}
}
