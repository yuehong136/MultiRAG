package handler_test

import "testing"

func TestGoogleProviderLiveHTTPAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	for _, instance := range []string{"one", "two"} {
		t.Run(instance, func(t *testing.T) { exerciseCanonicalNativeChat(t, f, "Google", "gemini-2.5-flash", instance) })
	}
}
