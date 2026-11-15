package handler_test

import "testing"

func TestAfterD7ProvidersHTTPCLIAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	t.Run("Xinference/served-chat", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "Xinference", "served-chat", instance) })
		}
	})
	t.Run("TogetherAI/openai/gpt-oss-20b", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "TogetherAI", "openai/gpt-oss-20b", instance) })
		}
	})
	t.Run("302.AI/gpt-4o", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "302.AI", "gpt-4o", instance) })
		}
	})
	t.Run("JieKouAI/zai-org/glm-4.5", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "JieKouAI", "zai-org/glm-4.5", instance) })
		}
	})
	t.Run("CometAPI/gpt-4o", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "CometAPI", "gpt-4o", instance) })
		}
	})
	t.Run("302.AI/embedding", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalInference(t, f, "302.AI", "jina-embeddings-v3", instance, "embedding")
			})
		}
	})
	t.Run("302.AI/rerank", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalInference(t, f, "302.AI", "jina-reranker-v2-base-multilingual", instance, "rerank")
			})
		}
	})
	t.Run("JieKouAI/embedding", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalInference(t, f, "JieKouAI", "text-embedding-3-large", instance, "embedding")
			})
		}
	})
	t.Run("JieKouAI/rerank", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalInference(t, f, "JieKouAI", "baai/bge-reranker-v2-m3", instance, "rerank")
			})
		}
	})
	t.Run("CometAPI/embedding", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalInference(t, f, "CometAPI", "text-embedding-3-small", instance, "embedding")
			})
		}
	})
	t.Run("Replicate/chat", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalNativeChat(t, f, "Replicate", "meta/meta-llama-3-70b-instruct", instance)
			})
		}
	})
	t.Run("302.AI/speech2text", func(t *testing.T) { exerciseCanonicalMedia(t, f, "302.AI", "whisper-v3-turbo", "one", "speech2text") })
	t.Run("CometAPI/speech2text", func(t *testing.T) { exerciseCanonicalMedia(t, f, "CometAPI", "whisper-1", "one", "speech2text") })
	t.Run("CometAPI/tts", func(t *testing.T) { exerciseCanonicalMedia(t, f, "CometAPI", "tts-1", "one", "tts") })
}
