package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/cloudwego/eino/schema"
	"google.golang.org/genai"
	"net/http"

	"reflect"
	"strings"
	"testing"
)

func multimodalFixture() []Message {
	return []Message{{Role: "system", Content: "rules"}, {Role: "assistant", Content: "old"}, {Role: "user", Content: []any{map[string]any{"type": "text", "text": "describe"}, map[string]any{"type": "image_url", "image_url": map[string]any{"url": "https://example.com/a.png", "detail": "high"}}}}}
}

func TestMultimodalProviderContract(t *testing.T) {
	for _, name := range []string{"aliyun", "deepseek", "gitee", "siliconflow", "zhipu-ai", "minimax", "moonshot", "vllm", "volcengine"} {
		t.Run(name, func(t *testing.T) {
			calls := 0
			responseBody := `{"choices":[{"message":{"content":"answer","reasoning_content":"reason"}}]}`
			server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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
			configuration := &ChatConfig{Thinking: &on}
			response, err := driver.ChatWithMessages("fixture", multimodalFixture(), &APIConfig{ApiKey: &key}, configuration)
			if err != nil || response == nil || response.Answer == nil || *response.Answer != "answer" {
				t.Fatalf("response=%+v err=%v", response, err)
			}
			if name == "deepseek" || name == "minimax" || name == "gitee" || name == "siliconflow" {
				if response.ReasonContent == nil || *response.ReasonContent != "reason" {
					t.Fatal("reasoning lost")
				}
			}
			for _, bad := range []string{`{"choices":[]}`, `{"choices":[{}]}`, `{"choices":[{"message":{"content":null}}]}`, `{"error":{"message":"failed"}}`} {
				responseBody = bad
				if _, err := driver.ChatWithMessages("fixture", multimodalFixture(), &APIConfig{ApiKey: &key}, nil); err == nil {
					t.Fatalf("accepted %s", bad)
				}
			}
			before := calls

			if _, err := driver.ChatWithMessages("fixture", nil, &APIConfig{ApiKey: &key}, nil); err == nil {
				t.Fatal("accepted empty messages")
			}
			if calls != before {
				t.Fatal("invalid requests reached provider")
			}
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			if _, err := driver.ChatWithMessages("fixture", multimodalFixture(), &APIConfig{ApiKey: &key, Context: ctx}, nil); !errors.Is(err, context.Canceled) {
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
			server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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
			if err := driver.ChatStreamlyWithSender("fixture", history, &APIConfig{ApiKey: &key}, nil, sender); err != nil {
				t.Fatal(err)
			}
			if answer.String() != "answer" || reason.String() != "reason" || done != 1 {
				t.Fatalf("stream %q %q %d", answer.String(), reason.String(), done)
			}
			want := errors.New("sender failed")
			if err := driver.ChatStreamlyWithSender("fixture", history, &APIConfig{ApiKey: &key}, nil, func(*string, *string) error { return want }); !errors.Is(err, want) {
				t.Fatalf("sender failure %v", err)
			}
			for _, bad := range []string{"data: {bad}\n\n", "data: {\"choices\":[]}\n\ndata: [DONE]\n\n", "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n", "data: {\"error\":{}}\n\ndata: [DONE]\n\n"} {
				payload = bad
				done = 0
				if err := driver.ChatStreamlyWithSender("fixture", history, &APIConfig{ApiKey: &key}, nil, sender); err == nil || done != 0 {
					t.Fatalf("bad stream accepted %q: %v done=%d", bad, err, done)
				}
			}
		})
	}
}

func TestGoogleMultimodalConversion(t *testing.T) {
	messages := multimodalFixture()
	messages[2].Content = []map[string]any{{"type": "text", "text": "describe"}, {"type": "image_url", "image_url": map[string]any{"url": "data:image/png;base64,AQID"}}, {"type": "image_url", "image_url": map[string]any{"url": "https://example.com/a.webp"}}}
	calls := 0
	server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		var body struct {
			Contents          []*genai.Content            `json:"contents"`
			SystemInstruction *genai.Content              `json:"systemInstruction"`
			GenerationConfig  genai.GenerateContentConfig `json:"generationConfig"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		if body.SystemInstruction == nil || body.SystemInstruction.Parts[0].Text != "rules" || body.GenerationConfig.MaxOutputTokens != 123 || len(body.Contents) != 2 || body.Contents[0].Role != "model" || len(body.Contents[1].Parts) != 3 {
			t.Errorf("history/config lost %+v", body)
			return
		}
		parts := body.Contents[1].Parts
		if parts[1].InlineData.MIMEType != "image/png" || !reflect.DeepEqual(parts[1].InlineData.Data, []byte{1, 2, 3}) || parts[2].FileData.FileURI != "https://example.com/a.webp" {
			t.Error("image bytes/MIME lost")
		}
		fmt.Fprint(w, `{"candidates":[{"content":{"parts":[{"text":"answer"}]}}]}`)
	}))
	driver := NewGoogleModel(map[string]string{"default": server.URL}, URLSuffix{})
	tokens := 123
	if _, err := driver.ChatWithMessages("gemini-test", messages, &APIConfig{ApiKey: stringPtr("fixture")}, &ChatConfig{MaxTokens: &tokens}); err != nil {
		t.Fatal(err)
	}
	for _, raw := range []string{"data:image/png;base64,?", "file:///tmp/a.png", "", "https://"} {
		invalid := []Message{{Role: "user", Content: []any{map[string]any{"type": "image_url", "image_url": map[string]any{"url": raw}}}}}
		if _, err := driver.ChatWithMessages("gemini-test", invalid, &APIConfig{ApiKey: stringPtr("fixture")}, nil); err == nil {
			t.Fatalf("accepted %q", raw)
		}
	}
	if calls != 1 {
		t.Fatal("invalid image reached HTTP", calls)
	}
}

type emptyChatDriver struct {
	ModelDriver
	response *ChatResponse
}

func (d *emptyChatDriver) ChatWithMessages(string, []Message, *APIConfig, *ChatConfig) (*ChatResponse, error) {
	return d.response, nil
}
func TestBoundChatRejectsEmptyResponse(t *testing.T) {
	for _, response := range []*ChatResponse{nil, {}, {Answer: stringPtr("")}} {
		name := "fixture"
		bound := NewChatModel(&emptyChatDriver{response: response}, &name, nil)
		if _, err := NewEinoChatModel(bound, nil).Generate(context.Background(), []*schema.Message{{Role: schema.User, Content: "q"}}); err == nil {
			t.Fatalf("accepted %#v", response)
		}
	}
}

func TestURLMediaValidationAndGoogleBoundary(t *testing.T) {
	for _, kind := range []string{"video_url", "file_url"} {
		messages := []Message{{Role: "user", Content: []any{map[string]any{"type": kind, kind: map[string]any{"url": "https://example.com/media"}}}}}

		body, err := captureChatBodyForTest(t, "VLLM", "vision", messages, nil, true)
		rawMessages, _ := json.Marshal(messages)
		var expected any
		json.Unmarshal(rawMessages, &expected)
		if err != nil || !reflect.DeepEqual(body["messages"], expected) {
			t.Fatal("media payload changed", err)
		}
		if _, err := NewGoogleModel(nil, URLSuffix{}).ChatWithMessages("gemini-test", messages, &APIConfig{ApiKey: stringPtr("fixture")}, nil); err == nil {
			t.Fatal("Google silently accepted unsupported media")
		}
	}
}
