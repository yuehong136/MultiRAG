package handler_test

import "testing"

func TestNVIDIASiliconFlowEmbeddingLiveCLIHTTPAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	t.Run("NVIDIA/embedding", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalInference(t, f, "NVIDIA", "nvidia/nv-embedqa-e5-v5", instance, "embedding")
			})
		}
	})
	t.Run("SiliconFlow/embedding", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalInference(t, f, "SiliconFlow", "BAAI/bge-m3", instance, "embedding")
			})
		}
	})
}
