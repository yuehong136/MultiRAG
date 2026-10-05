package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
)

func multimodalFixture() []Message {
	return []Message{{Role: "system", Content: "rules"}, {Role: "assistant", Content: "old", ReasoningContent: stringPtr("earlier reasoning")}, {Role: "user", Content: []any{map[string]any{"type": "text", "text": "describe"}, map[string]any{"type": "image_url", "image_url": map[string]any{"url": "https://example.com/a.png", "detail": "high"}}}}}
}

func TestMultimodalProviderContract(t *testing.T) {
	for _, name := range []string{"aliyun", "deepseek", "gitee", "siliconflow", "zhipu-ai", "minimax", "moonshot", "vllm", "volcengine"} {
		t.Run(name, func(t *testing.T) {
			calls := 0
			responseBody := `{"choices":[{"message":{"content":"answer","reasoning_content":"reason"}}]}`
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				var body struct {
					Messages []Message
					Stream   bool
					Model    string
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Fatal(err)
				}
				if body.Stream || body.Model != "fixture" || !reflect.DeepEqual(body.Messages, multimodalFixture()) {
					t.Errorf("history changed: %+v", body)
				}
				fmt.Fprint(w, responseBody)
			}))
			defer server.Close()
			driver, err := NewModelFactory().CreateModelDriver(name, map[string]string{"default": server.URL}, URLSuffix{Chat: "chat/completions"})
			if err != nil {
				t.Fatal(err)
			}
			key, on := "fixture", true
			response, err := driver.ChatWithMessages("fixture", &APIConfig{APIKey: &key}, multimodalFixture(), &ChatConfig{Stream: &on, Thinking: &on})
			if err != nil || response == nil || response.Answer == nil || *response.Answer != "answer" {
				t.Fatalf("response=%+v err=%v", response, err)
			}
			if name == "deepseek" || name == "minimax" || name == "gitee" || name == "siliconflow" {
				if response.ReasoningContent == nil || *response.ReasoningContent != "reason" {
					t.Fatal("reasoning lost")
				}
			}
			for _, bad := range []string{`{"choices":[]}`, `{"choices":[{}]}`, `{"choices":[{"message":{"content":null}}]}`, `{"error":{"message":"failed"}}`} {
				responseBody = bad
				if _, err := driver.ChatWithMessages("fixture", &APIConfig{APIKey: &key}, multimodalFixture(), nil); err == nil {
					t.Fatalf("accepted %s", bad)
				}
			}
			before := calls
			for _, content := range []any{nil, 42, map[string]any{"text": "x"}, []any{}, []any{true}, []any{map[string]any{"type": "audio"}}, []any{map[string]any{"type": "text", "text": false}}} {
				if _, err := driver.ChatWithMessages("fixture", &APIConfig{APIKey: &key}, []Message{{Role: "user", Content: content}}, nil); err == nil {
					t.Fatalf("accepted %#v", content)
				}
			}
			if _, err := driver.ChatWithMessages("fixture", &APIConfig{APIKey: &key}, nil, nil); err == nil {
				t.Fatal("accepted empty messages")
			}
			if calls != before {
				t.Fatal("invalid requests reached provider")
			}
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			if _, err := driver.ChatWithMessages("fixture", &APIConfig{APIKey: &key, Context: ctx}, multimodalFixture(), nil); !errors.Is(err, context.Canceled) {
				t.Fatalf("cancellation lost: %v", err)
			}
		})
	}
}

func TestNewHistoryStreamProviders(t *testing.T) {
	for _, name := range []string{"aliyun", "deepseek", "gitee", "siliconflow", "zhipu-ai", "minimax", "moonshot", "vllm", "volcengine"} {
		t.Run(name, func(t *testing.T) {
			history := multimodalFixture()
			payload := "data: {\"choices\":[{\"delta\":{\"content\":\"answer\",\"reasoning_content\":\"reason\"}}]}\n\ndata: [DONE]\n\n"
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body struct {
					Messages []Message
					Stream   bool
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Fatal(err)
				}
				if !body.Stream || !reflect.DeepEqual(body.Messages, history) {
					t.Errorf("history lost %+v", body)
				}
				fmt.Fprint(w, payload)
			}))
			defer server.Close()
			driver, _ := NewModelFactory().CreateModelDriver(name, map[string]string{"default": server.URL}, URLSuffix{Chat: "chat/completions"})
			key := "fixture"
			var answer, reason strings.Builder
			done := 0
			sender := func(text, thought *string) error {
				if text != nil {
					if *text == "[DONE]" {
						done++
					} else {
						answer.WriteString(*text)
					}
				}
				if thought != nil {
					reason.WriteString(*thought)
				}
				return nil
			}
			if err := driver.ChatStreamlyWithMessages("fixture", history, &APIConfig{APIKey: &key}, nil, sender); err != nil {
				t.Fatal(err)
			}
			if answer.String() != "answer" || reason.String() != "reason" || done != 1 {
				t.Fatalf("stream %q %q %d", answer.String(), reason.String(), done)
			}
			want := errors.New("sender failed")
			if err := driver.ChatStreamlyWithMessages("fixture", history, &APIConfig{APIKey: &key}, nil, func(*string, *string) error { return want }); !errors.Is(err, want) {
				t.Fatalf("sender failure %v", err)
			}
			for _, bad := range []string{"data: {bad}\n\n", "data: {\"choices\":[]}\n\ndata: [DONE]\n\n", "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n", "data: {\"error\":{}}\n\ndata: [DONE]\n\n"} {
				payload = bad
				done = 0
				if err := driver.ChatStreamlyWithMessages("fixture", history, &APIConfig{APIKey: &key}, nil, sender); err == nil || done != 0 {
					t.Fatalf("bad stream accepted %q: %v done=%d", bad, err, done)
				}
			}
		})
	}
}

func TestGoogleMultimodalConversion(t *testing.T) {
	messages := multimodalFixture()
	messages[2].Content = []map[string]any{{"type": "text", "text": "describe"}, {"type": "image_url", "image_url": map[string]any{"url": "data:image/png;base64,AQID"}}, {"type": "image_url", "image_url": map[string]any{"url": "https://example.com/a.webp"}}}
	contents, generation, err := googleHistory(messages, &ChatConfig{MaxTokens: func() *int { v := 123; return &v }()})
	if err != nil {
		t.Fatal(err)
	}
	if generation.SystemInstruction.Parts[0].Text != "rules" || generation.MaxOutputTokens != 123 || len(contents) != 2 || contents[0].Role != "model" || len(contents[1].Parts) != 3 {
		t.Fatalf("history/config lost %+v %+v", contents, generation)
	}
	parts := contents[1].Parts
	if parts[1].InlineData.MIMEType != "image/png" || !reflect.DeepEqual(parts[1].InlineData.Data, []byte{1, 2, 3}) || parts[2].FileData.FileURI != "https://example.com/a.webp" {
		t.Fatal("image conversion lost MIME or bytes")
	}
	for _, raw := range []string{"data:image/png;base64,?", "file:///tmp/a.png", "", "https://"} {
		if _, err := ContentParts([]any{map[string]any{"type": "image_url", "image_url": map[string]any{"url": raw}}}); err == nil {
			t.Fatalf("accepted %q", raw)
		}
	}
}

type emptyChatDriver struct {
	ModelDriver
	response *ChatResponse
}

func (d *emptyChatDriver) ChatWithMessages(string, *APIConfig, []Message, *ChatConfig) (*ChatResponse, error) {
	return d.response, nil
}
func TestBoundChatRejectsEmptyResponse(t *testing.T) {
	for _, response := range []*ChatResponse{nil, {}, {Answer: stringPtr("")}} {
		name := "fixture"
		bound := NewChatModel(&emptyChatDriver{response: response}, &name, nil)
		if _, err := bound.ChatWithMessages([]Message{{Role: "user", Content: "q"}}, nil); err == nil {
			t.Fatalf("accepted %#v", response)
		}
	}
}
