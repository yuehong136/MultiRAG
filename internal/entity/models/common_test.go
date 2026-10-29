package models

import "testing"

func TestGetThinkingAndAnswer(t *testing.T) {
	tests := []struct {
		name          string
		content       *string
		modelClass    *string
		wantReasoning *string
		wantAnswer    *string
	}{
		{name: "nil content", content: nil, modelClass: stringPtr("qwen3")},
		{name: "plain answer", content: stringPtr("answer"), modelClass: stringPtr("gpt"), wantAnswer: stringPtr("answer")},
		{name: "think tags", content: stringPtr("<think>reason</think>answer"), modelClass: stringPtr("qwen3"), wantReasoning: stringPtr("reason"), wantAnswer: stringPtr("answer")},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			reasoning, answer := GetThinkingAndAnswer(tt.modelClass, tt.content)
			assertStringPointer(t, reasoning, tt.wantReasoning)
			assertStringPointer(t, answer, tt.wantAnswer)
		})
	}
}

func assertStringPointer(t *testing.T, got, want *string) {
	t.Helper()
	if got == nil || want == nil {
		if got != want {
			t.Fatalf("got %v, want %v", got, want)
		}
		return
	}
	if *got != *want {
		t.Fatalf("got %q, want %q", *got, *want)
	}
}

func stringPtr(value string) *string { return &value }

func TestNormalizeModelFamily(t *testing.T) {
	for _, tt := range []struct {
		input *string
		want  string
	}{
		{nil, ""},
		{stringPtr("  "), ""},
		{stringPtr("qwen3"), "qwen3"},
		{stringPtr("qwen3-8b"), "qwen3"},
		{stringPtr(" qwen/Qwen3.5-4B "), "qwen3"},
		{stringPtr("namespace/vendor/qwen3-8b"), "qwen3"},
		{stringPtr("qwen3.5"), "qwen3"},
		{stringPtr("qwen-plus"), "qwen"},
		{stringPtr("qwen/qwen2.5-32b-instruct"), "qwen2.5"},
		{stringPtr("deepseek/deepseek-r1"), "deepseek"},
		{stringPtr("qwen30-8b"), "qwen30"},
		{stringPtr("qwen3foo-8b"), "qwen3foo"},
		{stringPtr("qwen3_8b"), "qwen3_8b"},
		{stringPtr("qwen/"), ""},
	} {
		name := "nil"
		if tt.input != nil {
			name = *tt.input
		}
		t.Run(name, func(t *testing.T) {
			if got := NormalizeModelFamily(tt.input); got != tt.want {
				t.Fatalf("NormalizeModelFamily=%q, want %q", got, tt.want)
			}
			if tt.input != nil && ModelClassFromName(*tt.input) != tt.want {
				t.Fatal("model-name and explicit-class normalization diverged")
			}
		})
	}
}

func TestThinkingFamilyExtractionBoundaries(t *testing.T) {
	content := "<think>\nplan</think>\nanswer"
	for _, family := range []string{"qwen3", "qwen3-8b", "qwen/qwen3-8b", " Qwen/Qwen3.5-4B "} {
		t.Run(family, func(t *testing.T) {
			reasoning, answer := GetThinkingAndAnswer(&family, &content)
			assertStringPointer(t, reasoning, stringPtr("plan"))
			assertStringPointer(t, answer, stringPtr("answer"))
		})
	}
	for _, family := range []*string{nil, stringPtr("deepseek/deepseek-r1"), stringPtr("qwen-plus"), stringPtr("qwen/qwen2.5-32b"), stringPtr("qwen30"), stringPtr("qwen3foo"), stringPtr("qwen3_8b")} {
		reasoning, answer := GetThinkingAndAnswer(family, &content)
		if reasoning != nil || answer != &content {
			t.Fatalf("unknown family %v changed original content pointer", family)
		}
	}
	if reason, answer := extractThinkContent(nil); reason != nil || answer != nil {
		t.Fatal("nil helper input did not return nils")
	}
}

func TestChatModelClassNormalizesExplicitAndDerivedFamilies(t *testing.T) {
	for _, config := range []*ChatConfig{nil, {}, {ModelClass: stringPtr("  ")}, {ModelClass: stringPtr(" Qwen/Qwen3.5-4B ")}} {
		if got := chatModelClass("Qwen/Qwen3.5-4B", config); got != "qwen3" {
			t.Fatalf("class=%q", got)
		}
	}
	if got := chatModelClass("qwen/qwen3-8b", &ChatConfig{ModelClass: stringPtr("qwen30")}); got != "qwen30" {
		t.Fatal("explicit class was ignored", got)
	}
}
