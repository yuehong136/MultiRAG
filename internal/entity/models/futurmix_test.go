package models

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestFuturMixChatAndSSE(t *testing.T) {
	server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer controlled-key" || r.URL.Path != "/v1/chat/completions" {
			t.Error("endpoint/key")
		}
		if r.Header.Get("Accept") == "text/event-stream" {
			fmt.Fprint(w, "data: {\"choices\":[{\"delta\":\n"+"data: {\"reasoning_content\":\"thought\",\"content\":\"answer\"}}]}\n\n"+"data: {\"choices\":[{\"finish_reason\":\"stop\"}]}\n\n")
		} else {
			fmt.Fprint(w, `{"choices":[{"message":{"content":"answer","reasoning_content":"thought"},"finish_reason":"stop"}]}`)
		}
	}))
	defer server.Close()
	key := "controlled-key"
	api := &APIConfig{APIKey: &key}
	history := []Message{{Role: "system", Content: "rules"}, {Role: "user", Content: "q"}}
	driver, err := NewModelFactory().CreateModelDriver("FuturMix", map[string]string{"default": server.URL}, URLSuffix{Chat: "v1/chat/completions"})
	if err != nil {
		t.Fatal(err)
	}
	name := "m"
	result, err := NewChatModel(driver, &name, api).ChatWithMessages(history, nil)
	if err != nil || *result.Answer != "answer" || *result.ReasoningContent != "thought" {
		t.Fatal(result, err)
	}
	var chunks []string
	err = driver.ChatStreamlyWithMessages("m", history, api, nil, func(c, r *string) error {
		if r != nil {
			chunks = append(chunks, *r)
		}
		if c != nil {
			chunks = append(chunks, *c)
		}
		return nil
	})
	if err != nil || !reflect.DeepEqual(chunks, []string{"thought", "answer", "[DONE]"}) {
		t.Fatal(chunks, err)
	}
	if _, err := driver.ListModels(api); err == nil {
		t.Fatal("invented discovery")
	}
	if err := driver.CheckConnection(api); err == nil {
		t.Fatal("invented connection check")
	}
	if _, err := driver.Embed(&name, []string{"x"}, api, nil); err == nil {
		t.Fatal("invented embedding")
	}
	if _, err := driver.Rerank(&name, "x", []string{"x"}, api, nil); err == nil {
		t.Fatal("invented rerank")
	}
}
func TestFuturMixRawFailures(t *testing.T) {
	for _, body := range []string{`{}`, `{"error":"private-secret","choices":[{"message":{"content":"answer"}}]}`, `{"choices":[{"message":{"content":null}}]}`, `{"choices":[{"message":{"content":"answer"},"finish_reason":"content_filter"}]}`, `{"choices":[{"message":{"content":"answer"}}]} {}`} {
		server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
		key := "key"
		driver := NewFuturMixModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
		if result, err := driver.ChatWithMessages("m", &APIConfig{APIKey: &key}, []Message{{Role: "user", Content: "q"}}, nil); err == nil || result != nil || strings.Contains(err.Error(), "private-secret") {
			t.Fatal(result, err)
		}
		server.Close()
	}
	for _, body := range []string{"data: {}\n\n", "data: {bad}\n\n", "data: {\"error\":\"private-secret\"}\n\n", "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n", "data: [DONE]\n\n", "data: {\"choices\":[{\"delta\":{\"content\":\"a\"},\"finish_reason\":\"tool_calls\"}]}\n\n"} {
		server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
		key := "key"
		driver := NewFuturMixModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
		done := false
		err := driver.ChatStreamlyWithMessages("m", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: &key}, nil, func(c, r *string) error { done = done || c != nil && *c == "[DONE]"; return nil })
		if err == nil || done || strings.Contains(err.Error(), "private-secret") {
			t.Fatal(err, done)
		}
		server.Close()
	}
}
func TestFuturMixStreamLifecycle(t *testing.T) {
	key := "key"
	messages := []Message{{Role: "user", Content: "q"}}
	for _, mode := range []string{"idle", "cancel", "sender"} {
		t.Run(mode, func(t *testing.T) {
			server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				if mode != "idle" {
					fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\n")
				}
				w.(http.Flusher).Flush()
				<-r.Context().Done()
			}))
			defer server.Close()
			driver := NewFuturMixModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
			driver.streamIdleTimeout = 40 * time.Millisecond
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			want := errors.New("sender stopped")
			err := driver.ChatStreamlyWithMessages("m", messages, &APIConfig{APIKey: &key, Context: ctx}, nil, func(c, r *string) error {
				if mode == "sender" {
					return want
				}
				cancel()
				return nil
			})
			if mode == "sender" && !errors.Is(err, want) || mode == "cancel" && !errors.Is(err, context.Canceled) || mode == "idle" && !errors.Is(err, context.DeadlineExceeded) {
				t.Fatal(err)
			}
		})
	}
}

func TestFuturMixConcurrentInstances(t *testing.T) {
	var wg sync.WaitGroup
	for _, id := range []string{"one", "two"} {
		server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Header.Get("Authorization") != "Bearer "+id {
				t.Error("credential crossed instances")
			}
			fmt.Fprint(w, `{"choices":[{"message":{"content":"`+id+`"}}]}`)
		}))
		urls := map[string]string{"default": server.URL}
		driver := NewFuturMixModel(urls, URLSuffix{Chat: "chat"})
		urls["default"] = "http://unused.invalid"
		for i := 0; i < 8; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				result, err := driver.ChatWithMessages("m", &APIConfig{APIKey: &id}, []Message{{Role: "user", Content: "q"}}, nil)
				if err != nil || *result.Answer != id {
					t.Error(result, err)
				}
			}()
		}
	}
	wg.Wait()
}
