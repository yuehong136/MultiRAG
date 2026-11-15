package handler_test

import "testing"

func TestAfterF9ProvidersHTTPCLIAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	t.Run("Astraflow/claude-opus-4-7", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "Astraflow", "claude-opus-4-7", instance) })
		}
	})
	t.Run("Groq/openai/gpt-oss-20b", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "Groq", "openai/gpt-oss-20b", instance) })
		}
	})
	t.Run("HunYuan/hunyuan-pro", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "HunYuan", "hunyuan-pro", instance) })
		}
	})
	t.Run("TokenHub/gpt-4o-mini", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "TokenHub", "gpt-4o-mini", instance) })
		}
	})
	t.Run("Astraflow/embedding", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalInference(t, f, "Astraflow", "text-embedding-3-large", instance, "embedding")
			})
		}
	})
	t.Run("Astraflow/rerank", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalInference(t, f, "Astraflow", "bge-reranker-v2-m3", instance, "rerank")
			})
		}
	})
	t.Run("HunYuan/embedding", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalInference(t, f, "HunYuan", "hunyuan-embedding", instance, "embedding")
			})
		}
	})
	t.Run("TokenHub/embedding", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalInference(t, f, "TokenHub", "fixture-embedding", instance, "embedding")
			})
		}
	})
	t.Run("Groq/speech2text", func(t *testing.T) { exerciseCanonicalMedia(t, f, "Groq", "whisper-large-v3", "one", "speech2text") })
	t.Run("Groq/tts", func(t *testing.T) {
		exerciseCanonicalMedia(t, f, "Groq", "canopylabs/orpheus-v1-english", "one", "tts")
	})
	t.Run("Astraflow/tts", func(t *testing.T) { exerciseCanonicalMedia(t, f, "Astraflow", "IndexTeam/IndexTTS-2", "one", "tts") })
}
