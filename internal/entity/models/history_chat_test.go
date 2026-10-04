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

func TestHistoryChatProtocolsAndCancellation(t *testing.T) {
	for _, provider := range []string{"Aliyun", "Moonshot", "VolcEngine", "Zhipu-AI"} {
		t.Run(provider, func(t *testing.T) {
			history := []Message{{Role: "system", Content: "rules"}, {Role: "user", Content: "old"}, {Role: "assistant", Content: "reply"}, {Role: "user", Content: "question"}}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/region/chat" || r.Header.Get("Authorization") != "Bearer fixture-key" {
					t.Errorf("request %s credential mismatch", r.URL.Path)
				}
				var body struct {
					Messages       []map[string]string `json:"messages"`
					Stream         bool                `json:"stream"`
					Thinking       json.RawMessage     `json:"thinking"`
					EnableThinking *bool               `json:"enable_thinking"`
					Temperature    float64             `json:"temperature"`
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				var got []Message
				for _, m := range body.Messages {
					got = append(got, Message{Role: m["role"], Content: m["content"]})
				}
				if !reflect.DeepEqual(got, history) || body.Temperature != 0 {
					t.Errorf("history/config=%#v", body)
				}
				if provider == "Aliyun" {
					if body.EnableThinking == nil || *body.EnableThinking {
						t.Error("explicit false lost")
					}
				} else if !strings.Contains(string(body.Thinking), "disabled") {
					t.Error("explicit false lost")
				}
				if body.Stream {
					fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\",\"reasoning_content\":\"reason\"}}]}\n\ndata: [DONE]\n\n")
				} else {
					fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
				}
			}))
			defer server.Close()
			driver, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": "http://127.0.0.1:1", "region": server.URL + "/region"}, URLSuffix{Chat: "chat"})
			if err != nil {
				t.Fatal(err)
			}
			key, name, region, off, zero := "fixture-key", "model", "region", false, 0.0
			api := &APIConfig{APIKey: &key, Region: &region}
			answer, err := driver.ChatWithMessages(name, api, history, &ChatConfig{Thinking: &off, Temperature: &zero})
			if err != nil || answer == nil || answer.Answer == nil || *answer.Answer != "answer" {
				t.Fatalf("history chat=%v err=%v", answer, err)
			}
			var events []string
			err = driver.ChatStreamlyWithMessages(name, history, api, &ChatConfig{Thinking: &off, Temperature: &zero}, func(content, reason *string) error {
				if content != nil {
					events = append(events, *content)
				}
				if reason != nil {
					events = append(events, "reason:"+*reason)
				}
				return nil
			})
			if err != nil || len(events) != 3 || events[len(events)-1] != "[DONE]" {
				t.Fatalf("stream=%v err=%v", events, err)
			}
			want := errors.New("sender failed")
			err = driver.ChatStreamlyWithMessages(name, history, api, &ChatConfig{Thinking: &off, Temperature: &zero}, func(*string, *string) error { return want })
			if !errors.Is(err, want) {
				t.Fatalf("sender=%v", err)
			}
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			api.Context = ctx
			if _, err := driver.ChatWithMessages(name, api, history, nil); !errors.Is(err, context.Canceled) {
				t.Fatalf("sync cancellation=%v", err)
			}
			if err := driver.ChatStreamlyWithMessages(name, history, api, nil, func(*string, *string) error { return nil }); !errors.Is(err, context.Canceled) {
				t.Fatalf("stream cancellation=%v", err)
			}
		})
	}
}

func TestBoundHistoryStreamingArrivesBeforeProviderCompletes(t *testing.T) {
	received := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n")
		w.(http.Flusher).Flush()
		<-r.Context().Done()
		close(received)
	}))
	defer server.Close()
	name, key := "kimi-k2.5", "key"
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	model := NewChatModel(NewMoonshotModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"}), &name, &APIConfig{APIKey: &key, Context: ctx})
	chunks := make(chan string, 1)
	finished := make(chan error, 1)
	go func() {
		finished <- model.ChatStreamlyWithSender("rules", []map[string]string{{"role": "user", "content": "question"}}, nil, func(content, reason *string) error {
			if content != nil {
				chunks <- *content
			}
			return nil
		})
	}()
	select {
	case chunk := <-chunks:
		if chunk != "partial" {
			t.Fatal(chunk)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no incremental answer")
	}
	cancel()
	select {
	case err := <-finished:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("cancel=%v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("stream did not cancel")
	}
	select {
	case <-received:
	case <-time.After(3 * time.Second):
		t.Fatal("provider request remained open")
	}
	select {
	case chunk := <-chunks:
		t.Fatalf("extra success after cancel=%s", chunk)
	default:
	}
}

func TestBoundHistoryRejectsTruncationAndInvalidConfig(t *testing.T) {
	for _, response := range []string{"data: [DONE]\n\n", "data: nope\n\n", "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n"} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, response) }))
		name, key := "model", "key"
		bound := NewChatModel(NewMoonshotModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"}), &name, &APIConfig{APIKey: &key})
		done := false
		err := bound.ChatStreamlyWithSender("", []map[string]string{{"role": "user", "content": "q"}}, nil, func(content, reason *string) error { done = content != nil && *content == "[DONE]"; return nil })
		server.Close()
		if err == nil || done {
			t.Fatalf("failed stream succeeded %v done=%v", err, done)
		}
	}
	off, on := false, true
	defaults := ChatConfig{Thinking: &on}
	c, err := chatGenerationConfig(nil, defaults)
	if err != nil || c.Stream != nil || c.Thinking == nil || !*c.Thinking {
		t.Fatalf("nil defaults=%#v %v", c, err)
	}
	c, err = chatGenerationConfig(map[string]interface{}{"stream": false, "thinking": false}, defaults)
	if err != nil || c.Stream == nil || *c.Stream != off || *c.Thinking {
		t.Fatalf("false=%#v %v", c, err)
	}
	c, err = chatGenerationConfig(map[string]interface{}{"effort": "high", "verbosity": "low", "stop": []interface{}{"stop"}, "max_tokens": 32.0}, defaults)
	if err != nil || c.Effort == nil || *c.Effort != "high" || c.Verbosity == nil || *c.Verbosity != "low" || c.Stop == nil || len(*c.Stop) != 1 || c.MaxTokens == nil || *c.MaxTokens != 32 {
		t.Fatalf("persisted JSON config=%#v %v", c, err)
	}
	if _, err := chatGenerationConfig(map[string]interface{}{"stream": "false"}, defaults); err == nil {
		t.Fatal("invalid boolean accepted")
	}
	if _, err := chatGenerationConfig(map[string]interface{}{"stream": func() {}}, defaults); err == nil {
		t.Fatal("invalid JSON accepted")
	}
}
