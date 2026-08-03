package models

import "testing"

func TestGetThinkingAndAnswer(t *testing.T) {
	tests := []struct {
		name          string
		content       *string
		modelSeries   *string
		wantReasoning *string
		wantAnswer    *string
	}{
		{name: "nil content", content: nil, modelSeries: stringPtr("qwen3")},
		{name: "plain answer", content: stringPtr("answer"), modelSeries: stringPtr("gpt"), wantAnswer: stringPtr("answer")},
		{name: "think tags", content: stringPtr("<think>reason</think>answer"), modelSeries: stringPtr("qwen3"), wantReasoning: stringPtr("reason"), wantAnswer: stringPtr("answer")},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			reasoning, answer := GetThinkingAndAnswer(tt.modelSeries, tt.content)
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
