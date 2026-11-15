package handler_test

import "testing"

func TestAfter4776ProvidersHTTPCLIAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	for _, instance := range []string{"one", "two"} {
		t.Run("Bedrock/"+instance, func(t *testing.T) {
			exerciseCanonicalNativeChat(t, f, "Bedrock", "anthropic.claude-3-5-sonnet-20241022-v2:0", instance)
		})
		t.Run("FuturMix/"+instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "FuturMix", "gpt-5.5", instance) })
	}
	t.Run("ollama/chat", func(t *testing.T) { exerciseCanonicalNativeChat(t, f, "ollama", "owned-chat", "one") })
	t.Run("ollama/embedding", func(t *testing.T) { exerciseCanonicalInference(t, f, "ollama", "owned-embed", "one", "embedding") })
	t.Run("DeepInfra/rerank", func(t *testing.T) { exerciseCanonicalInference(t, f, "DeepInfra", "owned/rerank", "one", "rerank") })
}
