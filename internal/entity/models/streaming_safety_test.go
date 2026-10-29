package models

import (
	"google.golang.org/genai"
	"testing"
)

func TestGoogleTextNestedEmptyResponses(t *testing.T) {
	for _, response := range []*genai.GenerateContentResponse{nil, {}, {Candidates: []*genai.Candidate{nil}}, {Candidates: []*genai.Candidate{{}}}, {Candidates: []*genai.Candidate{{Content: &genai.Content{}}}}, {Candidates: []*genai.Candidate{{Content: &genai.Content{Parts: []*genai.Part{nil}}}}}} {
		if a, r := googleText(response, true); a != "" || r != "" {
			t.Fatal(a, r)
		}
	}
	response := &genai.GenerateContentResponse{Candidates: []*genai.Candidate{{Content: &genai.Content{Parts: []*genai.Part{nil, {Text: "r", Thought: true}, {Text: "a"}, nil, {Text: "b"}}}}}}
	if a, r := googleText(response, true); a != "ab" || r != "r" {
		t.Fatal(a, r)
	}
	if a, r := googleText(response, false); a != "ab" || r != "" {
		t.Fatal(a, r)
	}
}

func TestOllamaMultipleTextPartsArePreserved(t *testing.T) {
	body, err := ollamaBody("m", []Message{{Role: "user", Content: []any{map[string]any{"type": "text", "text": "first"}, map[string]any{"type": "text", "text": "second"}}}}, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	messages := body["messages"].([]map[string]any)
	if messages[0]["content"] != "first\nsecond" {
		t.Fatal(messages)
	}
	for _, content := range []any{17, []any{map[string]any{"type": "text", "text": 17}}, []any{nil}} {
		if _, err := ollamaBody("m", []Message{{Role: "user", Content: content}}, nil, false); err == nil {
			t.Fatal("invalid content accepted", content)
		}
	}
}
