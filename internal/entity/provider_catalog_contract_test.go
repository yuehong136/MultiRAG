package entity

import (
	"testing"

	"multirag/internal/entity/models"
)

func TestOpenRouterAndVolcEngineCatalogContracts(t *testing.T) {
	manager := newCatalogManager(t)
	requireCatalogProvider(t, manager, "OpenRouter", (*models.OpenRouterModel)(nil), models.URLSuffix{
		Chat: "chat/completions", Models: "models", Embedding: "embeddings", Rerank: "rerank", Balance: "credits", TTS: "audio/speech",
	})
	// Only the named reasoning chat models opt in to thinking. TTS must remain
	// visible as TTS without becoming a chat model or inheriting thinking defaults.
	requireCatalogModels(t, manager, "OpenRouter", []catalogModelContract{
		{"google/gemma-4-31b-it", []string{"chat"}, &ModelThinking{DefaultValue: true, ClearThinking: true}},
		{"minimax/minimax-m2.5", []string{"chat"}, &ModelThinking{DefaultValue: true, ClearThinking: true}},
		{"tencent/hy3-preview", []string{"chat"}, &ModelThinking{DefaultValue: true, ClearThinking: true}},
		{"openai/gpt-audio-mini", []string{"tts"}, nil},
	})
	requireCatalogProvider(t, manager, "VolcEngine", (*models.VolcEngine)(nil), models.URLSuffix{
		Chat: "chat/completions", Files: "files", Models: "models", Embedding: "embeddings/multimodal",
	})
	requireCatalogModels(t, manager, "VolcEngine", []catalogModelContract{
		{"doubao-seed-2-0-pro-260215", []string{"chat"}, &ModelThinking{DefaultValue: true, ClearThinking: true}},
		{"doubao-embedding-vision-250615", []string{"embedding"}, nil},
		{"doubao-embedding-vision-251215", []string{"embedding"}, nil},
	})
	for _, name := range []string{"doubao-embedding-vision-250615", "doubao-embedding-vision-251215"} {
		model, err := manager.GetModelByName("VolcEngine", name)
		if err != nil || model.MaxTokens != 131072 {
			t.Fatal("VolcEngine embedding context window changed", name, model, err)
		}
	}
}
