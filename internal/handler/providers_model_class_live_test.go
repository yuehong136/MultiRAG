package handler_test

import "testing"

func TestModelClassProviderLiveHTTPAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	t.Run("Gitee/qwen3-8b", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "Gitee", "qwen3-8b", instance+"-qwen") })
		}
	})
	t.Run("SiliconFlow/qwen/qwen3-8b", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "SiliconFlow", "qwen/qwen3-8b", instance) })
		}
	})
	t.Run("Gitee/glm-4.7-flash", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, "Gitee", "glm-4.7-flash", instance+"-glm") })
		}
	})
}
