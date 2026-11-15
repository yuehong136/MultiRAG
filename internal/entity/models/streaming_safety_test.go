package models

import (
	"encoding/json"
	"google.golang.org/genai"
	"net/http"
	"testing"
)

func TestGoogleTextNestedEmptyResponses(t *testing.T) {
	responses := []*genai.GenerateContentResponse{nil, {}, {Candidates: []*genai.Candidate{nil}}, {Candidates: []*genai.Candidate{{}}}, {Candidates: []*genai.Candidate{{Content: &genai.Content{}}}}, {Candidates: []*genai.Candidate{{Content: &genai.Content{Parts: []*genai.Part{nil}}}}}}
	var response *genai.GenerateContentResponse
	server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { json.NewEncoder(w).Encode(response) }))
	driver := NewGoogleModel(map[string]string{"default": server.URL}, URLSuffix{})
	api := &APIConfig{ApiKey: stringPtr("fixture")}
	for _, raw := range responses {
		response = raw
		if value, err := driver.ChatWithMessages("gemini-test", []Message{{Role: "user", Content: "q"}}, api, nil); err == nil || value != nil {
			t.Fatal("empty provider response accepted", value, err)
		}
	}
	response = &genai.GenerateContentResponse{Candidates: []*genai.Candidate{{Content: &genai.Content{Parts: []*genai.Part{{Text: "r", Thought: true}, {Text: "a"}, {Text: "b"}}}}}}
	for _, thinking := range []bool{true, false} {
		value, err := driver.ChatWithMessages("gemini-test", []Message{{Role: "user", Content: "q"}}, api, &ChatConfig{Thinking: &thinking})
		if err != nil || value == nil || *value.Answer != "ab" {
			t.Fatal(value, err)
		}
		want := ""
		if thinking {
			want = "r"
		}
		if value.ReasonContent == nil || *value.ReasonContent != want {
			t.Fatal("thought selection lost", value)
		}
	}
}

func TestOllamaMultipleTextPartsArePreserved(t *testing.T) {
	body, err := captureChatBodyForTest(t, "Ollama", "m", []Message{{Role: "user", Content: []any{map[string]any{"type": "text", "text": "first"}, map[string]any{"type": "text", "text": "second"}}}}, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	rawMessages := body["messages"].([]any)
	messages := []map[string]any{rawMessages[0].(map[string]any)}
	if messages[0]["content"] != "first\nsecond" {
		t.Fatal(messages)
	}
	for _, content := range []any{17, []any{map[string]any{"type": "text", "text": 17}}, []any{nil}} {
		if _, err := captureChatBodyForTest(t, "Ollama", "m", []Message{{Role: "user", Content: content}}, nil, false); err == nil {
			t.Fatal("invalid content accepted", content)
		}
	}
}
