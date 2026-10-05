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

func TestVLLMModelChatAndDiscovery(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer key" {
			t.Error("incorrect credential")
		}
		switch r.URL.Path {
		case "/api/chat":
			var body map[string]interface{}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Error(err)
			}
			if body["stream"] != false || body["chat_template_kwargs"].(map[string]interface{})["enable_thinking"] != true {
				t.Errorf("body = %#v", body)
			}
			fmt.Fprint(w, `{"choices":[{"message":{"content":"answer","reasoning_content":"reason"}}]}`)
		case "/api/models":
			if r.Method != http.MethodGet || r.ContentLength > 0 {
				t.Error("model discovery requires GET without a body")
			}
			fmt.Fprint(w, `{"data":[{"id":"doubao"}]}`)
		case "/api/files":
			fmt.Fprint(w, `{"data":[]}`)
		default:
			t.Errorf("wrong path %s", r.URL.Path)
			w.WriteHeader(404)
		}
	}))
	defer server.Close()
	driver, err := NewModelFactory().CreateModelDriver("vllm", map[string]string{"default": "http://invalid.invalid", "fixture": server.URL + "/api/"}, URLSuffix{Chat: "chat", Models: "models", Files: "files"})
	if err != nil {
		t.Fatal(err)
	}
	key, name, message, region, thinking := "key", "doubao", "question", "fixture", true
	config := &APIConfig{APIKey: &key, Region: &region}
	response, err := driver.ChatWithMessages(name, config, []Message{{Role: "user", Content: message}}, &ChatConfig{Thinking: &thinking})
	if err != nil || response == nil || *response.Answer != "answer" || *response.ReasoningContent != "reason" {
		t.Fatalf("response = %#v, %v", response, err)
	}
	names, err := driver.ListModels(config)
	if err != nil || !reflect.DeepEqual(names, []string{"doubao"}) {
		t.Fatalf("models = %v, %v", names, err)
	}
	if err := driver.CheckConnection(config); err != nil {
		t.Fatal(err)
	}
	if _, err := driver.Encode(&name, []string{"q"}, config, nil); err == nil {
		t.Fatal("unsupported embedding succeeded")
	}
}

func TestVLLMModelStreamContracts(t *testing.T) {
	tests := []struct {
		name, response string
		wantErr        bool
	}{
		{"combined delta", "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"reason\",\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n", false},
		{"nil config", "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n", false},
		{"empty stream", "data: [DONE]\n\n", true},
		{"truncated stream", "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\n", true},
		{"bad JSON", "data: nope\n\n", true},
		{"business error", "data: {\"error\":{\"message\":\"secret\"}}\n\n", true},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body map[string]interface{}
				json.NewDecoder(r.Body).Decode(&body)
				if body["stream"] != true {
					t.Error("stream flag lost")
				}
				fmt.Fprint(w, test.response)
			}))
			defer server.Close()
			driver := NewVLLMModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
			key, name, message := "key", "doubao", "question"
			var frames []string
			err := driver.ChatStreamlyWithSender(&name, &message, &APIConfig{APIKey: &key}, nil, func(content, reason *string) error {
				if reason != nil {
					frames = append(frames, "reason:"+*reason)
				}
				if content != nil {
					frames = append(frames, *content)
				}
				return nil
			})
			if (err != nil) != test.wantErr {
				t.Fatalf("error = %v", err)
			}
			if test.name == "combined delta" && !reflect.DeepEqual(frames, []string{"reason:reason", "answer", "[DONE]"}) {
				t.Fatalf("frames = %v", frames)
			}
			if test.wantErr && len(frames) > 0 && frames[len(frames)-1] == "[DONE]" {
				t.Fatal("error produced completion frame")
			}
		})
	}
}

func TestVLLMModelValidationSenderErrorAndCancellation(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
	}))
	defer server.Close()
	driver := NewVLLMModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
	key, name, message := "key", "doubao", "question"
	failure := errors.New("sender failed")
	err := driver.ChatStreamlyWithSender(&name, &message, &APIConfig{APIKey: &key}, nil, func(*string, *string) error { return failure })
	if !errors.Is(err, failure) {
		t.Fatalf("error = %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	err = driver.ChatStreamlyWithSender(&name, &message, &APIConfig{APIKey: &key, Context: ctx}, nil, func(*string, *string) error { return nil })
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("cancel error = %v", err)
	}
	if _, err := driver.ChatWithMessages("", nil, []Message{{Role: "user", Content: message}}, nil); err == nil {
		t.Fatal("nil arguments accepted")
	}

}

func TestVLLMModelLargeStreamDelta(t *testing.T) {
	answer := strings.Repeat("a", 100000)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":%q}}]}\n\ndata: [DONE]\n\n", answer)
	}))
	defer server.Close()
	driver := NewVLLMModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
	key, name, message := "key", "doubao", "q"
	var got string
	err := driver.ChatStreamlyWithSender(&name, &message, &APIConfig{APIKey: &key}, nil, func(content, reason *string) error {
		if content != nil && *content != "[DONE]" {
			got += *content
		}
		return nil
	})
	if err != nil || got != answer {
		t.Fatalf("large delta: len=%d error=%v", len(got), err)
	}
	if _, err := driver.chat(name, nil, &APIConfig{APIKey: &key}, nil); err == nil {
		t.Fatal("empty messages accepted")
	}
}

func TestVLLMModelKeylessAndThinkingFalse(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" {
			t.Error("unexpected key")
		}
		var body map[string]interface{}
		json.NewDecoder(r.Body).Decode(&body)
		if body["thinking"] != nil || body["enable_thinking"] != nil || body["chat_template_kwargs"].(map[string]interface{})["enable_thinking"] != false {
			t.Errorf("%v", body)
		}
		messages := body["messages"].([]interface{})
		if len(messages) != 3 || messages[0].(map[string]interface{})["role"] != "system" {
			t.Error("role history lost")
		}
		fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
	}))
	defer server.Close()
	driver := NewVLLMModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat/completions"})
	thinking := false
	_, err := driver.ChatWithMessages("Qwen/test", nil, []Message{{Role: "system", Content: "rules"}, {Role: "assistant", Content: "old"}, {Role: "user", Content: "q"}}, &ChatConfig{Thinking: &thinking})
	if err != nil {
		t.Fatal(err)
	}
}

func TestVLLMDiscoveryErrors(t *testing.T) {
	for _, payload := range []string{`not-json`, `{"error":{"message":"failure"}}`, `{"data":"invalid"}`} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, payload) }))
		driver := NewVLLMModel(map[string]string{"default": server.URL}, URLSuffix{Models: "models"})
		if _, err := driver.ListModels(nil); err == nil {
			t.Errorf("accepted malformed discovery: %s", payload)
		}
		server.Close()
	}
}
