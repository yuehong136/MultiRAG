package entity

import (
	"testing"

	"multirag/internal/entity/models"
)

func TestAddedProviderCatalogsAndFactory(t *testing.T) {
	manager := newCatalogManager(t)
	for _, test := range []struct {
		provider string
		driver   models.ModelDriver
		suffix   models.URLSuffix
		models   []catalogModelContract
	}{
		{
			"CoHere", (*models.CoHereModel)(nil),
			models.URLSuffix{Chat: "v2/chat", Models: "v1/models", Rerank: "v2/rerank", Embedding: "v2/embed", ASR: "audio/transcriptions"},
			[]catalogModelContract{
				{"command-a-03-2025", []string{"chat"}, nil},
				{"rerank-v4.0-pro", []string{"rerank"}, nil},
				{"embed-v4.0", []string{"embedding"}, nil},
				{"cohere-transcribe-03-2026", []string{"speech2text"}, nil},
			},
		},
		{
			"FishAudio", (*models.FishAudioModel)(nil),
			models.URLSuffix{Models: "model", Balance: "wallet/self/api-credit", TTS: "v1/tts", ASR: "v1/asr"},
			[]catalogModelContract{
				{"s2-pro", []string{"tts"}, nil},
				{"s1", []string{"tts"}, nil},
				{"transcribe-1", []string{"asr"}, nil},
			},
		},
		{
			"StepFun", (*models.StepFunModel)(nil),
			models.URLSuffix{Chat: "chat/completions", Models: "models", TTS: "audio/speech"},
			[]catalogModelContract{
				{"step-3.5-flash", []string{"chat"}, nil},
				{"step-1v-32k", []string{"chat", "vision"}, nil},
				{"step-tts-2", []string{"tts"}, nil},
				{"stepaudio-2.5-tts", []string{"tts"}, nil},
				{"step-tts-mini", []string{"tts"}, nil},
			},
		},
		{
			"Baichuan", (*models.BaichuanModel)(nil),
			models.URLSuffix{Chat: "chat/completions", Embedding: "embeddings"},
			[]catalogModelContract{
				{"Baichuan4", []string{"chat"}, nil},
				{"Baichuan-Text-Embedding", []string{"embedding"}, nil},
			},
		},
		{
			"Upstage", (*models.UpstageModel)(nil),
			models.URLSuffix{Chat: "chat/completions", Models: "models", Embedding: "embeddings"},
			[]catalogModelContract{
				{"solar-pro3", []string{"chat"}, nil},
				{"solar-embedding-1-large-query", []string{"embedding"}, nil},
				{"solar-embedding-1-large-passage", []string{"embedding"}, nil},
			},
		},
	} {
		t.Run(test.provider, func(t *testing.T) {
			requireCatalogProvider(t, manager, test.provider, test.driver, test.suffix)
			requireCatalogModels(t, manager, test.provider, test.models)
		})
	}
}
