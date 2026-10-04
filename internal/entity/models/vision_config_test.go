package models

import "testing"

func TestVisionGenerationConfiguration(t *testing.T) {
	enabled := true
	defaults := ChatConfig{Vision: &enabled}
	for _, values := range []map[string]interface{}{nil, {"vision": false}} {
		config, err := chatGenerationConfig(values, defaults)
		want := values == nil
		if err != nil || config.Vision == nil || *config.Vision != want || !*defaults.Vision {
			t.Fatalf("vision override: %#v %v", config, err)
		}
	}
	if _, err := chatGenerationConfig(map[string]interface{}{"vision": "false"}, defaults); err == nil {
		t.Fatal("invalid vision value accepted")
	}
}
