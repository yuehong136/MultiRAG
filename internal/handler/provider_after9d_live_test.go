package handler_test

import "testing"

func TestAfter9dProvidersHTTPCLIAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	t.Run("Novita/deepseek/deepseek-v4-pro", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "Novita", "deepseek/deepseek-v4-pro", instance) })
		}
	})
	t.Run("Jina/jina-vlm", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "Jina", "jina-vlm", instance) })
		}
	})
	t.Run("Anthropic/claude-sonnet-4-5-20250929", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "Anthropic", "claude-sonnet-4-5-20250929", instance) })
		}
	})
	t.Run("CometAPI/gpt-4o", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "CometAPI", "gpt-4o", instance) })
		}
	})
	t.Run("SiliconFlow/qwen/qwen3-8b", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "SiliconFlow", "qwen/qwen3-8b", instance) })
		}
	})
	t.Run("DeepInfra/deepseek-ai/DeepSeek-V3.2", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "DeepInfra", "deepseek-ai/DeepSeek-V3.2", instance) })
		}
	})
	t.Run("XunFei/spark-x", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "XunFei", "spark-x", instance) })
		}
	})
	t.Run("Novita/embedding", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalInference(t, f, "Novita", "baai/bge-m3", instance, "embedding") })
		}
	})
	t.Run("CometAPI/embedding", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalInference(t, f, "CometAPI", "text-embedding-3-small", instance, "embedding")
			})
		}
	})
	t.Run("SiliconFlow/embedding", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalInference(t, f, "SiliconFlow", "Qwen/Qwen3-Embedding-0.6B", instance, "embedding")
			})
		}
	})
	t.Run("DeepInfra/embedding", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalInference(t, f, "DeepInfra", "Qwen/Qwen3-Embedding-4B", instance, "embedding")
			})
		}
	})
}
