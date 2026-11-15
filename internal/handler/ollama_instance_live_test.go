package handler_test

import "testing"

func TestOllamaInstanceLiveHTTPAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	for _, instance := range []string{"one", "two"} {
		t.Run(instance, func(t *testing.T) { exerciseCanonicalNativeChat(t, f, "ollama", "qwen3", instance) })
	}
}
