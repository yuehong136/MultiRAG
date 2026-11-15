package handler_test

import "testing"

func TestAfter440ProvidersHTTPCLIAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	t.Run("Avian/deepseek/deepseek-v4-pro", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "Avian", "deepseek/deepseek-v4-pro", instance) })
		}
	})
	t.Run("Astraflow/claude-opus-4-7", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "Astraflow", "claude-opus-4-7", instance) })
		}
	})
	t.Run("Perplexity/sonar", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "Perplexity", "sonar", instance) })
		}
	})
	t.Run("Mistral/magistral-medium-latest", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "Mistral", "magistral-medium-latest", instance) })
		}
	})
	t.Run("Upstage/solar-pro3", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "Upstage", "solar-pro3", instance) })
		}
	})
	t.Run("Xinference/local-chat", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "Xinference", "local-chat", instance) })
		}
	})
	t.Run("Novita/deepseek/deepseek-v4-pro", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "Novita", "deepseek/deepseek-v4-pro", instance) })
		}
	})
	t.Run("Moonshot/kimi-k2.6", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "Moonshot", "kimi-k2.6", instance) })
		}
	})
	t.Run("Perplexity/embedding", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalInference(t, f, "Perplexity", "pplx-embed-v1-0.6b", instance, "embedding")
			})
		}
	})
	t.Run("Replicate/embedding", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalInference(t, f, "Replicate", "replicate/all-mpnet-base-v2:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", instance, "embedding")
			})
		}
	})
	t.Run("Xinference/speech2text", func(t *testing.T) { exerciseCanonicalMedia(t, f, "Xinference", "local-asr", "one", "speech2text") })
	t.Run("Xinference/tts", func(t *testing.T) { exerciseCanonicalMedia(t, f, "Xinference", "local-tts", "one", "tts") })
}
