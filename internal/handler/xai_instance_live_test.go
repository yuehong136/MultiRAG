package handler_test

import "testing"

func TestXAIInstanceLiveHTTPAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	for _, instance := range []string{"one", "two"} {
		t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "xAI", "grok-test", instance) })
	}
}
