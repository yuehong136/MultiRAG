package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

func TestOllamaChatDiscoveryAndStreaming(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/tags" {
			if r.Method != "GET" || r.ContentLength > 0 {
				t.Error("invalid discovery")
			}
			fmt.Fprint(w, `{"models":[{"name":"qwen3:latest"}]}`)
			return
		}
		if r.URL.Path != "/api/chat" {
			t.Errorf("wrong endpoint %s", r.URL.Path)
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		messages := body["messages"].([]any)
		if len(messages) != 3 || messages[1].(map[string]any)["thinking"] != "previous thought" {
			t.Error("history lost")
		}
		last := messages[2].(map[string]any)
		if last["content"] != "first\nsecond" || len(last["images"].([]any)) != 1 || body["think"] != false || body["options"].(map[string]any)["temperature"] != float64(0) {
			t.Errorf("content/options %v", body)
		}
		if body["stream"] == true {
			fmt.Fprintln(w, `{"message":{"content":"answer","thinking":"reason"},"done":false}`)
			fmt.Fprintln(w, `{"done":true}`)
		} else {
			fmt.Fprint(w, `{"message":{"content":"answer","thinking":"reason"},"done":true}`)
		}
	}))
	defer server.Close()
	driver, err := NewModelFactory().CreateModelDriver("Ollama", map[string]string{"default": server.URL}, URLSuffix{Chat: "api/chat", Models: "api/tags"})
	if err != nil {
		t.Fatal(err)
	}
	messages := []Message{{Role: "system", Content: "rules"}, {Role: "assistant", Content: "previous", ReasoningContent: stringPtr("previous thought")}, {Role: "user", Content: []any{map[string]any{"type": "text", "text": "first"}, map[string]any{"type": "text", "text": "second"}, map[string]any{"type": "image_url", "image_url": map[string]any{"url": "data:image/png;base64,aGVsbG8="}}}}}
	off, zero := false, 0.0
	config := &ChatConfig{Thinking: &off, Temperature: &zero}
	answer, err := driver.ChatWithMessages("qwen3:latest", nil, messages, config)
	if err != nil || *answer.Answer != "answer" || *answer.ReasoningContent != "reason" {
		t.Fatal(answer, err)
	}
	var frames []string
	if err := driver.ChatStreamlyWithMessages("qwen3:latest", messages, nil, config, func(c, r *string) error {
		if r != nil {
			frames = append(frames, *r)
		}
		if c != nil {
			frames = append(frames, *c)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(frames, []string{"reason", "answer", "[DONE]"}) {
		t.Fatal(frames)
	}
	if names, err := driver.ListModels(nil); err != nil || !reflect.DeepEqual(names, []string{"qwen3:latest"}) {
		t.Fatal(names, err)
	}
	if err := driver.CheckConnection(nil); err != nil {
		t.Fatal(err)
	}
	name := "m"
	if _, err := driver.Encode(&name, []string{"q"}, nil, nil); err == nil {
		t.Fatal("embedding stub succeeded")
	}
	if _, err := driver.Rerank(&name, "q", []string{"d"}, nil); err == nil {
		t.Fatal("rerank stub succeeded")
	}
}
func TestOllamaFailureContracts(t *testing.T) {
	for _, payload := range []string{`not-json`, `{"error":"private provider failure"}`, `{"done":true}`, `{"message":{"content":"partial"}}`} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, payload) }))
		driver := NewOllamaModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "api/chat", Models: "api/tags"})
		history := []Message{{Role: "user", Content: "q"}}
		done := false
		if _, err := driver.ChatWithMessages("m", nil, history, nil); err == nil {
			t.Error("bad response accepted")
		}
		err := driver.ChatStreamlyWithMessages("m", history, nil, nil, func(c, r *string) error {
			if c != nil && *c == "[DONE]" {
				done = true
			}
			return nil
		})
		if err == nil || done {
			t.Error("bad stream succeeded", err)
		}
		if _, err := driver.ListModels(nil); err == nil {
			t.Error("bad models accepted")
		}
		server.Close()
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"message":{"content":"answer"},"done":true}`)
	}))
	defer server.Close()
	driver := NewOllamaModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "api/chat"})
	history := []Message{{Role: "user", Content: "q"}}
	want := errors.New("sender failed")
	if err := driver.ChatStreamlyWithMessages("m", history, nil, nil, func(*string, *string) error { return want }); !errors.Is(err, want) {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := driver.ChatWithMessages("m", &APIConfig{Context: ctx}, history, nil); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if _, err := ollamaBody("m", multimodalFixture(), nil, false); err == nil {
		t.Fatal("remote image silently accepted")
	}
}
func TestMoonshotDefaultTemperature(t *testing.T) {
	for _, stream := range []bool{false, true} {
		for _, config := range []*ChatConfig{nil, {}} {
			body, err := moonshotChatBody("kimi", []Message{{Role: "user", Content: "q"}}, config, stream)
			if err != nil || body["temperature"] != 0.6 {
				t.Fatal(body, err)
			}
		}
	}
	zero := 0.0
	body, err := moonshotChatBody("kimi", []Message{{Role: "user", Content: "q"}}, &ChatConfig{Temperature: &zero}, false)
	if err != nil || body["temperature"] != zero {
		t.Fatal(body, err)
	}
}
