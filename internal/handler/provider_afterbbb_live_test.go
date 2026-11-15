package handler_test

import "testing"

func TestAfterBBBProvidersHTTPCLIAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	t.Run("LocalAI/embedding", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalInference(t, f, "LocalAI", "local-embed", instance, "embedding") })
		}
	})
	t.Run("LocalAI/rerank", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalInference(t, f, "LocalAI", "local-rerank", instance, "rerank") })
		}
	})
	t.Run("Voyage/embedding", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalInference(t, f, "Voyage", "voyage-3.5", instance, "embedding") })
		}
	})
	t.Run("Voyage/rerank", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalInference(t, f, "Voyage", "rerank-2", instance, "rerank") })
		}
	})
	t.Run("ZHIPU-AI/embedding", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalInference(t, f, "ZHIPU-AI", "embedding-3", instance, "embedding") })
		}
	})
	t.Run("LocalAI/chat", func(t *testing.T) { exerciseCanonicalChat(t, f, "LocalAI", "local-chat", "one") })
}
