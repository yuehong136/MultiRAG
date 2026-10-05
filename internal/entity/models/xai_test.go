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
	"time"
)

func TestXAIChatStreamDiscovery(t *testing.T) {
	for _, finish := range []bool{false, true} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Header.Get("Authorization") != "Bearer key" {
				t.Error("credential lost")
			}
			if r.URL.Path == "/v1/models" {
				if r.Method != "GET" || r.ContentLength > 0 {
					t.Error("invalid discovery")
				}
				fmt.Fprint(w, `{"data":[{"id":"grok-test"}]}`)
				return
			}
			if r.URL.Path != "/v1/chat/completions" {
				t.Error("endpoint", r.URL.Path)
			}
			var body struct {
				Messages    []Message
				Stream      bool
				Temperature float64
				MaxTokens   int `json:"max_tokens"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Error(err)
			}
			if !reflect.DeepEqual(body.Messages, multimodalFixture()) || body.Temperature != 0 || body.MaxTokens != 100 {
				t.Errorf("request changed %+v", body)
			}
			if !body.Stream {
				fmt.Fprint(w, `{"choices":[{"message":{"content":"answer","reasoning_content":"reason"}}]}`)
				return
			}
			fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"reason\",\"content\":\"answer\"}}]}\n\n")
			if finish {
				fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n")
			} else {
				fmt.Fprint(w, "data: [DONE]\n\n")
			}
		}))
		defer server.Close()
		driver, err := NewModelFactory().CreateModelDriver("xAI", map[string]string{"default": server.URL + "/v1"}, URLSuffix{Chat: "chat/completions", Models: "models"})
		if err != nil {
			t.Fatal(err)
		}
		key, zero, max, on := "key", 0.0, 100, true
		api := &APIConfig{APIKey: &key}
		config := &ChatConfig{Temperature: &zero, MaxTokens: &max, Stream: &on}
		response, err := driver.ChatWithMessages("grok-test", api, multimodalFixture(), config)
		if err != nil || *response.Answer != "answer" || *response.ReasoningContent != "reason" {
			t.Fatal(response, err)
		}
		off := false
		config.Stream = &off
		var frames []string
		err = driver.ChatStreamlyWithMessages("grok-test", multimodalFixture(), api, config, func(c, r *string) error {
			if r != nil {
				frames = append(frames, *r)
			}
			if c != nil {
				frames = append(frames, *c)
			}
			return nil
		})
		if err != nil || !reflect.DeepEqual(frames, []string{"reason", "answer", "[DONE]"}) {
			t.Fatal(frames, err)
		}
		if names, err := driver.ListModels(api); err != nil || !reflect.DeepEqual(names, []string{"grok-test"}) {
			t.Fatal(names, err)
		}
		if err := driver.CheckConnection(api); err != nil {
			t.Fatal(err)
		}
	}
}
func TestXAIFailuresAndCompletion(t *testing.T) {
	for _, test := range []struct {
		name, body string
		status     int
	}{
		{"http", "secret-body", 401}, {"bad-json", "invalid", 200}, {"empty", "{}", 200}, {"null", "null", 200}, {"provider-error", `{"error":{"message":"secret-body"}}`, 200},
		{"truncated", "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n", 200},
		{"empty-finish", "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n", 200},
		{"malformed-delta", "data: {\"choices\":[{\"delta\":{\"content\":42}}]}\n\n", 200},
		{"broken-after-answer", "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\ndata: nope\n\ndata: [DONE]\n\n", 200},
		{"empty-list", `{"data":[]}`, 200}, {"bad-list", `{"data":[{}]}`, 200},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(test.status); fmt.Fprint(w, test.body) }))
			defer server.Close()
			driver := NewXAIModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat", Models: "models"})
			key := "key"
			api := &APIConfig{APIKey: &key}
			history := []Message{{Role: "user", Content: "q"}}
			if _, err := driver.ChatWithMessages("m", api, history, nil); err == nil {
				t.Fatal("malformed answer accepted")
			}
			if err := driver.CheckConnection(api); err == nil {
				t.Fatal("malformed/empty discovery accepted")
			}
			done := false
			err := driver.ChatStreamlyWithMessages("m", history, api, nil, func(c, r *string) error {
				if c != nil && *c == "[DONE]" {
					done = true
				}
				return nil
			})
			if err == nil || done || strings.Contains(err.Error(), "secret-body") {
				t.Fatalf("false success or leaked body: %v done=%v", err, done)
			}
		})
	}
}
func TestXAICancellationAndSenderError(t *testing.T) {
	started := make(chan struct{}, 1)
	cancelled := make(chan struct{}, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\n")
		w.(http.Flusher).Flush()
		started <- struct{}{}
		<-r.Context().Done()
		cancelled <- struct{}{}
	}))
	defer server.Close()
	driver := NewXAIModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
	key := "key"
	history := []Message{{Role: "user", Content: "q"}}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	result := make(chan error, 1)
	go func() {
		result <- driver.ChatStreamlyWithMessages("m", history, &APIConfig{APIKey: &key, Context: ctx}, nil, func(*string, *string) error { cancel(); return nil })
	}()
	select {
	case <-started:
	case <-time.After(3 * time.Second):
		t.Fatal("not started")
	}
	if err := <-result; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	select {
	case <-cancelled:
	case <-time.After(3 * time.Second):
		t.Fatal("provider request not cancelled")
	}
	want := errors.New("sender failed")
	if err := driver.ChatStreamlyWithMessages("m", history, &APIConfig{APIKey: &key}, nil, func(*string, *string) error { return want }); !errors.Is(err, want) {
		t.Fatal(err)
	}
	if driver.httpClient.Timeout != 0 {
		t.Fatal("global timeout truncates long streams")
	}
	if _, err := driver.ChatWithMessages("m", nil, history, nil); err == nil {
		t.Fatal("missing key accepted")
	}
}
