package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestOpenAIChatHTTP(t *testing.T) { testHostedChatHTTP(t, "OpenAI", nil) }

// Also exercises factory construction against independent URLs and credentials.
func testHostedChatHTTP(t *testing.T, provider string, extra func(*testing.T, map[string]any)) {
	t.Helper()
	for _, instance := range []string{"one", "two"} {
		t.Run(instance, func(t *testing.T) {
			t.Parallel()
			key := "fixture-" + instance
			history := []Message{{Role: "system", Content: "rules"}, {Role: "assistant", Content: "previous", ReasoningContent: stringPtr("previous reason")}, {Role: "user", Content: []any{map[string]any{"type": "text", "text": "question"}, map[string]any{"type": "image_url", "image_url": map[string]any{"url": "https://example.invalid/image.png"}}}}}
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.Header.Get("Authorization") != "Bearer "+key {
					t.Error("credentials crossed instances")
				}
				if r.URL.Path == "/v1/models" {
					if r.Method != "GET" || r.ContentLength > 0 {
						t.Error("invalid discovery GET")
					}
					fmt.Fprintf(w, `{"data":[{"id":%q}]}`, instance)
					return
				}
				if r.Method != "POST" || r.URL.Path != "/v1/chat/completions" || r.Header.Get("Content-Type") != "application/json" {
					t.Error("invalid chat endpoint")
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				encoded, _ := json.Marshal(history)
				var expected any
				_ = json.Unmarshal(encoded, &expected)
				if !reflect.DeepEqual(body["messages"], expected) || body["model"] != instance || body["max_tokens"] != float64(16) || body["temperature"] != float64(0) || body["top_p"] != float64(0) || !reflect.DeepEqual(body["stop"], []any{"stop"}) {
					t.Error("history or options lost")
				}
				if extra != nil {
					extra(t, body)
				} else if body["thinking"] != nil || body["enable_thinking"] != nil || body["do_sample"] != nil {
					t.Error("unsupported OpenAI parameter emitted")
				}
				if body["stream"] == true {
					fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"reason\",\"content\":%q}}]}\n\ndata: {\"choices\":[],\"usage\":{\"total_tokens\":4}}\n\ndata: [DONE]\n\n", instance)
				} else {
					fmt.Fprintf(w, `{"choices":[{"message":{"content":%q,"reasoning_content":"reason"}}]}`, instance)
				}
			}))
			defer server.Close()
			urls := map[string]string{"default": server.URL + "/v1/", "office": server.URL + "/v1/"}
			driver, err := NewModelFactory().CreateModelDriver(provider, urls, URLSuffix{Chat: "/chat/completions", Models: "models"})
			if err != nil {
				t.Fatal(err)
			}
			urls["default"] = "http://mutated.invalid"
			region := "office"
			api := &APIConfig{APIKey: &key, Region: &region}
			off, on, zero, max := false, true, 0.0, 16
			stop := []string{"stop"}
			config := &ChatConfig{Stream: &on, Thinking: &off, Temperature: &zero, TopP: &zero, MaxTokens: &max, Stop: &stop, DoSample: &off}
			response, err := driver.ChatWithMessages(instance, api, history, config)
			if err != nil || response == nil || *response.Answer != instance || *response.ReasoningContent != "reason" {
				t.Fatal(response, err)
			}
			config.Stream = &off
			var frames []string
			if err := driver.ChatStreamlyWithMessages(instance, history, api, config, func(c, r *string) error {
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
			if !reflect.DeepEqual(frames, []string{"reason", instance, "[DONE]"}) {
				t.Fatal(frames)
			}
			if strings.EqualFold(provider, "OpenAI") {
				names, err := driver.ListModels(api)
				if err != nil || !reflect.DeepEqual(names, []string{instance}) {
					t.Fatal(names, err)
				}
				if err := driver.CheckConnection(api); err != nil {
					t.Fatal(err)
				}
				if calls.Load() != 4 {
					t.Fatal("missing discovery/connection HTTP calls")
				}
			} else if calls.Load() != 2 {
				t.Fatal("missing chat HTTP calls")
			}
			t.Logf("%s instance %s: %d authenticated HTTP calls, complete role/content/reasoning history, forced stream modes and isolated URL", provider, instance, calls.Load())
		})
	}
}

func TestOpenAIFailuresHTTP(t *testing.T) { testHostedChatFailures(t, "OpenAI", true) }
func testHostedChatFailures(t *testing.T, provider string, discovery bool) {
	t.Helper()
	answer := "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\n"
	cases := []struct {
		name, body, call string
		status           int
		valid            bool
	}{
		{"empty", "", "chat", 200, false}, {"bad JSON", "bad", "chat", 200, false}, {"missing choices", `{}`, "chat", 200, false}, {"null answer", `{"choices":[{"message":{"content":null}}]}`, "chat", 200, false}, {"empty answer", `{"choices":[{"message":{"content":" "}}]}`, "chat", 200, false}, {"provider error", `{"error":"private-body"}`, "chat", 200, false}, {"HTTP status", "private-body", "chat", 429, false},
		{"empty stream", "", "stream", 200, false}, {"done only", "data: [DONE]\n\n", "stream", 200, false}, {"bad event", "data: bad\n\n", "stream", 200, false}, {"bad after answer", answer + "data: bad\n\n", "stream", 200, false}, {"stream error", answer + "data: {\"error\":\"private-body\"}\n\n", "stream", 200, false}, {"truncated", answer, "stream", 200, false}, {"empty event", "data: {}\n\n", "stream", 200, false}, {"reason only", "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"r\"}}]}\n\ndata: [DONE]\n\n", "stream", 200, false}, {"finish reason", answer + "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n", "stream", 200, true}, {"large event", "data: {\"choices\":[{\"delta\":{\"content\":\"" + strings.Repeat("x", 128*1024) + "\"}}]}\n\ndata: [DONE]\n\n", "stream", 200, true}, {"oversize event", "data: " + strings.Repeat("x", 4*1024*1024) + "\n\ndata: [DONE]\n\n", "stream", 200, false},
	}
	if discovery {
		cases = append(cases, []struct {
			name, body, call string
			status           int
			valid            bool
		}{
			{"empty model inventory", `{"data":[]}`, "models", 200, true}, {"missing inventory", `{}`, "models", 200, false}, {"null inventory", `{"data":null}`, "models", 200, false}, {"null model", `{"data":[null]}`, "models", 200, false}, {"missing ID", `{"data":[{}]}`, "models", 200, false}, {"invalid ID", `{"data":[{"id":123}]}`, "models", 200, false}, {"models error", `{"data":[],"error":"private-body"}`, "models", 200, false}, {"models HTTP", "private-body", "models", 503, false}, {"connection error", "private-body", "connection", 403, false}, {"connection malformed", `{}`, "connection", 200, false}, {"connection empty inventory", `{"data":[]}`, "connection", 200, true},
		}...)
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(test.status); fmt.Fprint(w, test.body) }))
			defer server.Close()
			driver, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Chat: "chat/completions", Models: "models"})
			key := "fixture"
			api := &APIConfig{APIKey: &key}
			history := []Message{{Role: "user", Content: "q"}}
			done := 0
			var err error
			switch test.call {
			case "chat":
				_, err = driver.ChatWithMessages("m", api, history, nil)
			case "models":
				_, err = driver.ListModels(api)
			case "connection":
				err = driver.CheckConnection(api)
			case "stream":
				err = driver.ChatStreamlyWithMessages("m", history, api, nil, func(c, r *string) error {
					if c != nil && *c == "[DONE]" {
						done++
					}
					return nil
				})
			}
			if (err == nil) != test.valid || !test.valid && done != 0 || test.call == "stream" && test.valid && done != 1 {
				t.Fatalf("valid=%t err=%v done=%d", test.valid, err, done)
			}
			if err != nil && strings.Contains(err.Error(), "private-body") {
				t.Fatal("provider response leaked")
			}
			if test.name == "truncated" && !errors.Is(err, io.ErrUnexpectedEOF) {
				t.Fatal(err)
			}
		})
	}
}

func TestOpenAICancelHTTP(t *testing.T) { testHostedCancellation(t, "OpenAI", true) }
func testHostedCancellation(t *testing.T, provider string, discovery bool) {
	t.Helper()
	modes := []string{"cancel stream", "sender failure", "buffered DONE", "buffered finish", "reasoning failure", "terminal failure", "cancel chat"}
	if discovery {
		modes = append(modes, "cancel models", "cancel connection")
	}
	for _, mode := range modes {
		t.Run(mode, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			stopped := make(chan struct{})
			started := make(chan struct{})
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				defer close(stopped)
				close(started)
				if strings.HasPrefix(mode, "cancel") && mode != "cancel stream" {
					w.WriteHeader(200)
					w.(http.Flusher).Flush()
					<-r.Context().Done()
					return
				}
				fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\",\"reasoning_content\":\"reason\"}}]}\n\n")
				if mode == "buffered finish" {
					fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n")
				} else if mode != "cancel stream" && mode != "sender failure" {
					fmt.Fprint(w, "data: [DONE]\n\n")
				}
				w.(http.Flusher).Flush()
				if mode == "cancel stream" || mode == "sender failure" {
					<-r.Context().Done()
				}
			}))
			defer server.Close()
			driver, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Chat: "chat/completions", Models: "models"})
			key := "fixture"
			api := &APIConfig{APIKey: &key, Context: ctx}
			history := []Message{{Role: "user", Content: "q"}}
			sentinel := errors.New("delivery failed")
			done := false
			var err error
			if strings.HasPrefix(mode, "cancel") && mode != "cancel stream" {
				go func() { <-started; cancel() }()
				switch mode {
				case "cancel chat":
					_, err = driver.ChatWithMessages("m", api, history, nil)
				case "cancel models":
					_, err = driver.ListModels(api)
				case "cancel connection":
					err = driver.CheckConnection(api)
				}
			} else {
				err = driver.ChatStreamlyWithMessages("m", history, api, nil, func(c, r *string) error {
					terminal := c != nil && *c == "[DONE]"
					if mode == "sender failure" || mode == "reasoning failure" && r != nil || mode == "terminal failure" && terminal {
						return sentinel
					}
					if terminal {
						done = true
					}
					if c != nil && (mode == "cancel stream" || strings.HasPrefix(mode, "buffered")) {
						cancel()
					}
					return nil
				})
			}
			want := error(context.Canceled)
			if strings.Contains(mode, "failure") {
				want = sentinel
			}
			if !errors.Is(err, want) || done {
				t.Fatal(err, done)
			}
			select {
			case <-stopped:
			case <-time.After(time.Second):
				t.Fatal("HTTP request/body not released")
			}
		})
	}
}

func TestOpenAIInputAndLocalEmbedding(t *testing.T) {
	driver := NewOpenAIModel(map[string]string{"default": "http://fixture.invalid/v1"}, URLSuffix{Chat: "chat/completions", Models: "models"})
	history := []Message{{Role: "user", Content: "q"}}
	key, name := "fixture", "m"
	for _, api := range []*APIConfig{nil, {}, {APIKey: stringPtr(" ")}} {
		if _, err := driver.ChatWithMessages(name, api, history, nil); err == nil {
			t.Fatal("missing key accepted")
		}
		if err := driver.ChatStreamlyWithMessages(name, history, api, nil, func(*string, *string) error { return nil }); err == nil {
			t.Fatal("missing stream key accepted")
		}
	}
	api := &APIConfig{APIKey: &key}
	if _, err := driver.ChatWithMessages(name, api, nil, nil); err == nil {
		t.Fatal("empty history accepted")
	}
	if err := driver.ChatStreamlyWithMessages(name, history, api, nil, nil); err == nil {
		t.Fatal("nil sender accepted")
	}
	if err := driver.ChatStreamlyWithSender(nil, nil, api, nil, nil); err == nil {
		t.Fatal("nil inputs accepted")
	}
	if _, err := NewRerankModel(driver, &name, api).Rerank("q", []string{"d"}); err == nil {
		t.Fatal("rerank fabricated")
	}
	if _, err := driver.Balance(api); err == nil {
		t.Fatal("balance fabricated")
	}
	if _, err := driver.ChatStreamly(&name, &key, &name, nil); err == nil {
		t.Fatal("channel stream fabricated")
	}
	transport, ok := openAIChatTransport.(*http.Transport)
	if !ok || transport.ResponseHeaderTimeout != 60*time.Second || driver.httpClient.Timeout != 0 {
		t.Fatal("transport timeouts")
	}
	second := NewOpenAIModel(nil, URLSuffix{})
	if second.httpClient.Transport != driver.httpClient.Transport {
		t.Fatal("per-instance connection pool")
	}
	// The existing shared Embed contract suite exercises reordered and validated vectors through factory.
	if vectors, err := embeddingVectorsForTest(driver.Embed(&name, nil, nil, nil)); err != nil || len(vectors) != 0 {
		t.Fatal(vectors, err)
	}
}
