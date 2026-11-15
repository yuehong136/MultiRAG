package models

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"testing"

	"multirag/internal/testsupport"
)

const modelResponseLimitForTest = 20 * 1024 * 1024
const modelParamsLimitForTest = 64 * 1024

// Capture the actual frozen provider request, replacing former body-builder
// unit tests with a controlled HTTP consumer of the same assertion data.
func captureChatBodyForTest(t *testing.T, provider string, name string, messages []Message, cfg *ChatConfig, stream bool) (map[string]any, error) {
	t.Helper()
	var body map[string]any
	server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if provider == "Ollama" {
			fmt.Fprintln(w, `{"message":{"content":"answer"},"done":true}`)
		} else if stream {
			fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
		} else {
			fmt.Fprint(w, `{"choices":[{"message":{"content":"answer","reasoning_content":""}}]}`)
		}
	}))
	driver, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
	if err != nil {
		return nil, err
	}
	api := &APIConfig{ApiKey: stringPtr("fixture")}
	if stream {
		err = driver.ChatStreamlyWithSender(name, messages, api, cfg, func(*string, *string) error { return nil })
	} else {
		_, err = driver.ChatWithMessages(name, messages, api, cfg)
	}
	return body, err
}

func modelFixtureFile(t *testing.T, data []byte) *string {
	t.Helper()
	journal := testsupport.NewCase(t)
	path := filepath.Join(t.TempDir(), "model-input.wav")
	journal.Event("reserve-model-input", map[string]any{"path": path})
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
			t.Error(err)
		}
		_, err := os.Stat(path)
		absent := os.IsNotExist(err)
		journal.Event("model-input-cleanup-readback", map[string]any{"path": path, "absent": absent})
		if !absent {
			t.Error("model input remains", path)
		}
	})
	return &path
}

// The frozen sender exposes raw text/bytes through string pointers.
func modelStreamTextForTest(value *string) string {
	if value == nil || *value == "[DONE]" {
		return ""
	}
	return *value
}
func modelStreamBytesForTest(value *string) []byte {
	return []byte(modelStreamTextForTest(value))
}

// Project the typed response by its explicit indices, without validating it or
// manufacturing a driver error. Invalid-response tests still require the driver
// itself to return an error.
func rerankScoresForTest(bound *RerankModel, query string, documents []string, api *APIConfig, cfg *RerankConfig) ([]float64, error) {
	response, err := bound.Rerank(query, documents, api, cfg)
	if err != nil || response == nil {
		return nil, err
	}
	data := append([]RerankResult(nil), response.Data...)
	sort.SliceStable(data, func(i, j int) bool { return data[i].Index < data[j].Index })
	scores := make([]float64, len(data))
	for i, row := range data {
		scores[i] = row.RelevanceScore
	}
	return scores, nil
}

func joinModelNames(models []ListModelResponse, separator string) string {
	names := make([]string, len(models))
	for i, model := range models {
		names[i] = model.Name
	}
	return strings.Join(names, separator)
}

func modelNamesForTest(models []ListModelResponse) []string {
	names := make([]string, len(models))
	for i, model := range models {
		names[i] = model.Name
	}
	return names
}

func expectedRerankTopNForTest(count int, cfg *RerankConfig) (int, error) {
	if cfg == nil || cfg.TopN == 0 || cfg.TopN > count {
		return count, nil
	}
	if cfg.TopN < 0 {
		return 0, fmt.Errorf("negative top_n")
	}
	return cfg.TopN, nil
}

func readProviderConfig(t *testing.T, name string) []byte {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("..", "..", "..", "configs", "models", name))
	if err != nil {
		t.Fatal(err)
	}
	return data
}

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func messagesForTest(system string, history []map[string]string) []Message {
	messages := make([]Message, 0, len(history)+1)
	if system != "" {
		messages = append(messages, Message{Role: "system", Content: system})
	}
	for _, item := range history {
		messages = append(messages, Message{Role: item["role"], Content: item["content"]})
	}
	return messages
}

func chatAnswerForTest(bound *ChatModel, messages []Message, cfg *ChatConfig) (string, error) {
	response, err := bound.ModelDriver.ChatWithMessages(*bound.ModelName, messages, bound.APIConfig, cfg)
	if response == nil || response.Answer == nil {
		return "", err
	}
	return *response.Answer, err
}

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
			if tt.input != nil && NormalizeModelFamily(stringPtr(*tt.input)) != tt.want {
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
