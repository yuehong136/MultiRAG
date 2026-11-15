package handler_test

import "testing"

func TestAfter775ProvidersHTTPCLIAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	t.Run("PPIO/deepseek/deepseek-r1", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "PPIO", "deepseek/deepseek-r1", instance) })
		}
	})
	t.Run("TokenPony/qwen3-8b", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "TokenPony", "qwen3-8b", instance) })
		}
	})
	t.Run("Groq/llama-3.1-8b-instant", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "Groq", "llama-3.1-8b-instant", instance) })
		}
	})
	t.Run("TogetherAI/openai/gpt-oss-20b", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "TogetherAI", "openai/gpt-oss-20b", instance) })
		}
	})
	t.Run("ZHIPU-AI/glm-4-plus", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "ZHIPU-AI", "glm-4-plus", instance) })
		}
	})
	t.Run("OpenRouter/openai/gpt-4o-mini", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "OpenRouter", "openai/gpt-4o-mini", instance) })
		}
	})
	t.Run("TogetherAI/rerank", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalInference(t, f, "TogetherAI", "mixedbread-ai/mxbai-rerank-large-v2", instance, "rerank")
			})
		}
	})
	t.Run("TogetherAI/speech2text", func(t *testing.T) {
		exerciseCanonicalMedia(t, f, "TogetherAI", "openai/whisper-large-v3", "one", "speech2text")
	})
	t.Run("TogetherAI/tts", func(t *testing.T) {
		exerciseCanonicalMedia(t, f, "TogetherAI", "canopylabs/orpheus-3b-0.1-ft", "one", "tts")
	})
	t.Run("ZHIPU-AI/speech2text", func(t *testing.T) { exerciseCanonicalMedia(t, f, "ZHIPU-AI", "glm-asr-2512", "one", "speech2text") })
	t.Run("ZHIPU-AI/tts", func(t *testing.T) { exerciseCanonicalMedia(t, f, "ZHIPU-AI", "glm-tts", "one", "tts") })
}
