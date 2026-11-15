package handler_test

import "testing"

func TestModelInferenceLiveHTTPAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	t.Run("ZHIPU-AI/embedding", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalInference(t, f, "ZHIPU-AI", "embedding-3", instance, "embedding") })
		}
	})
	t.Run("ZHIPU-AI/rerank", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalInference(t, f, "ZHIPU-AI", "rerank", instance, "rerank") })
		}
	})
}
