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

func TestLMStudioChatAndReasoningHTTP(t *testing.T) {
	for _, mode := range []string{"omitted", "enabled", "disabled"} {
		t.Run(mode, func(t *testing.T) {
			history := []Message{{Role: "system", Content: "rules"}, {Role: "assistant", Content: "previous", ReasoningContent: stringPtr("previous thought")}, {Role: "user", Content: []any{map[string]any{"type": "text", "text": "question"}, map[string]any{"type": "image_url", "image_url": map[string]any{"url": "https://example.invalid/image.png"}}}}}
			off, on, zero, max := false, true, 0.0, 16
			stop := []string{"stop"}
			config := &ChatConfig{Stream: &on, MaxTokens: &max, Temperature: &zero, TopP: &zero, DoSample: &off, Stop: &stop}
			if mode == "enabled" {
				config.Thinking = &on
			}
			if mode == "disabled" {
				config.Thinking = &off
			}
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.Method != "POST" || r.URL.Path != "/v1/chat/completions" || r.Header.Get("Authorization") != "Bearer fixture-key" || r.Header.Get("Content-Type") != "application/json" {
					t.Errorf("invalid request: %s %s", r.Method, r.URL.Path)
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				expected := map[string]any{"model": body["model"], "messages": history, "stream": false, "temperature": zero, "top_p": zero, "max_tokens": max, "do_sample": off, "stop": stop}
				if mode != "omitted" {
					expected["thinking"] = map[string]string{"type": mode}
				}
				a, _ := json.Marshal(expected)
				b, _ := json.Marshal(body)
				var expectedJSON any
				if err := json.Unmarshal(a, &expectedJSON); err != nil {
					t.Error(err)
				}
				if !reflect.DeepEqual(expectedJSON, body) {
					t.Errorf("body = %s; want %s", b, a)
				}
				fmt.Fprint(w, `{"choices":[{"message":{"content":"answer","reasoning_content":"reason"}}]}`)
			}))
			defer server.Close()
			driver, err := NewModelFactory().CreateModelDriver("LM-Studio", map[string]string{"default": server.URL + "/v1/"}, URLSuffix{Chat: "/chat/completions"})
			if err != nil || driver.Name() != "LM-Studio" {
				t.Fatal(driver, err)
			}
			key := "fixture-key"
			for _, name := range []string{"qwen3-8b", "glm-4"} {
				response, err := driver.ChatWithMessages(name, &APIConfig{APIKey: &key}, history, config)
				if err != nil || response == nil || *response.Answer != "answer" || *response.ReasoningContent != "reason" {
					t.Fatal(response, err)
				}
			}
			if calls.Load() != 2 || !*config.Stream {
				t.Fatal("missing HTTP calls or config changed")
			}
			t.Log("POST /v1/chat/completions: full history/content parts, qwen/glm standard URL, stream=false, structured reasoning and generation options verified")
		})
	}
}

func TestLMStudioInlineReasoningAndNilConfig(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" {
			t.Error("unexpected authorization")
		}
		fmt.Fprint(w, `{"choices":[{"message":{"content":"<think>\nreason</think>\nanswer"}}]}`)
	}))
	defer server.Close()
	driver := NewLMStudioModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat/completions"})
	response, err := driver.ChatWithMessages("qwen3-8b", nil, []Message{{Role: "user", Content: "q"}}, nil)
	if err != nil || *response.Answer != "answer" || *response.ReasoningContent != "reason" {
		t.Fatal(response, err)
	}
}

func TestLMStudioStreamEndHTTP(t *testing.T) {
	for _, terminal := range []string{"data: [DONE]\n\n", "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n"} {
		t.Run(strings.Split(terminal, "\n")[0], func(t *testing.T) {
			content := strings.Repeat("a", 128*1024)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body struct {
					Stream   bool
					Thinking struct{ Type string }
					Messages []Message
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if r.URL.Path != "/v1/chat/completions" || !body.Stream || body.Thinking.Type != "disabled" || len(body.Messages) != 1 {
					t.Error("stream request contract")
				}
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprint(w, ": heartbeat\n\ndata: {\"choices\":[{\"delta\":{\"role\":\"assistant\"}}]}\n\n")
				fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"reason\",\"content\":%q}}]}\n\n", content)
				fmt.Fprint(w, "data: {\"choices\":[],\"usage\":{\"total_tokens\":4}}\n\n"+terminal)
			}))
			defer server.Close()
			driver := NewLMStudioModel(map[string]string{"default": server.URL + "/v1"}, URLSuffix{Chat: "chat/completions"})
			off, name, q := false, "glm-4", "question"
			var frames []string
			err := driver.ChatStreamlyWithSender(&name, &q, nil, &ChatConfig{Stream: &off, Thinking: &off}, func(c, r *string) error {
				if r != nil {
					frames = append(frames, *r)
				}
				if c != nil {
					frames = append(frames, *c)
				}
				return nil
			})
			if err != nil || !reflect.DeepEqual(frames, []string{"reason", content, "[DONE]"}) {
				t.Fatalf("stream err=%v frames=%d", err, len(frames))
			}
			if driver.httpClient.Timeout != 0 {
				t.Fatal("total SSE timeout")
			}
			t.Log("SSE: 128 KiB delta, reasoning, usage-only frame and exactly one terminal verified")
		})
	}
}

func TestLMStudioFailureHTTP(t *testing.T) {
	history := []Message{{Role: "user", Content: "q"}}
	for _, test := range []struct {
		name, body string
		stream     bool
		status     int
	}{
		{"empty", "", false, 200}, {"malformed", "not-json", false, 200}, {"no choices", `{"choices":[]}`, false, 200}, {"no message", `{"choices":[{}]}`, false, 200}, {"null content", `{"choices":[{"message":{"content":null}}]}`, false, 200}, {"empty answer", `{"choices":[{"message":{"content":""}}]}`, false, 200}, {"wrong type", `{"choices":[{"message":{"content":123}}]}`, false, 200}, {"whitespace", `{"choices":[{"message":{"content":"   "}}]}`, false, 200}, {"reason only", `{"choices":[{"message":{"content":"<think>r</think>"}}]}`, false, 200}, {"provider error", `{"error":"private-body"}`, false, 200}, {"http status", "private-body", false, 503},
		{"empty stream", "", true, 200}, {"done only", "data: [DONE]\n\n", true, 200}, {"malformed event", "data: invalid\n\n", true, 200}, {"missing choices", "data: {}\n\n", true, 200}, {"provider stream error", "data: {\"error\":\"private-body\"}\n\n", true, 200}, {"reasoning only", "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"r\"}}]}\n\ndata: [DONE]\n\n", true, 200}, {"truncated", "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n", true, 200}, {"bad after partial", "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\ndata: invalid\n\n", true, 200}, {"early finish", "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n", true, 200}, {"stream http status", "private-body", true, 500},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(test.status); fmt.Fprint(w, test.body) }))
			defer server.Close()
			driver := NewLMStudioModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat/completions"})
			var err error
			done := false
			if test.stream {
				err = driver.ChatStreamlyWithMessages("qwen3", history, nil, nil, func(c, r *string) error { done = done || (c != nil && *c == "[DONE]"); return nil })
			} else {
				_, err = driver.ChatWithMessages("qwen3", nil, history, nil)
			}
			if err == nil || done || strings.Contains(err.Error(), "private-body") {
				t.Fatal("failure accepted or private body exposed", err)
			}
			if test.name == "truncated" && !errors.Is(err, io.ErrUnexpectedEOF) {
				t.Fatal(err)
			}
		})
	}
}

func TestLMStudioListModelsHTTP(t *testing.T) {
	for _, test := range []struct {
		name, body string
		status     int
		valid      bool
	}{
		{"models", `{"data":[{"id":"one"},{"id":"two"}]}`, 200, true}, {"empty list", `{"data":[]}`, 200, true}, {"absent", `{}`, 200, false}, {"null", `{"data":null}`, 200, false}, {"bad JSON", `not-json`, 200, false}, {"bad shape", `{"data":{}}`, 200, false}, {"bad item", `{"data":[null]}`, 200, false}, {"bad ID", `{"data":[{"id":42}]}`, 200, false}, {"missing ID", `{"data":[{}]}`, 200, false}, {"whitespace ID", `{"data":[{"id":" "}]}`, 200, false}, {"error", `{"error":"private-body","data":[]}`, 200, false}, {"status", "private-body", 500, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != "GET" || r.URL.Path != "/v1/models" || r.ContentLength > 0 || r.Header.Get("Authorization") != "" {
					t.Error("discovery request")
				}
				w.WriteHeader(test.status)
				fmt.Fprint(w, test.body)
			}))
			defer server.Close()
			driver := NewLMStudioModel(map[string]string{"default": server.URL + "/v1/"}, URLSuffix{Models: "models"})
			names, err := driver.ListModels(nil)
			if (err == nil) != test.valid {
				t.Fatal(names, err)
			}
			if test.name == "models" && !reflect.DeepEqual(names, []string{"one", "two"}) {
				t.Fatal(names)
			}
			if test.name == "empty list" && (names == nil || len(names) != 0) {
				t.Fatal(names)
			}
		})
	}
}

func TestLMStudioCancelAndSenderFailure(t *testing.T) {
	for _, mode := range []string{"cancel streaming", "sender failure", "cancel normal", "cancel models"} {
		t.Run(mode, func(t *testing.T) {
			stopped := make(chan struct{})
			started := make(chan struct{})
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				if mode == "cancel streaming" || mode == "sender failure" {
					fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n")
				}
				w.(http.Flusher).Flush()
				close(started)
				<-r.Context().Done()
				close(stopped)
			}))
			defer server.Close()
			driver := NewLMStudioModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat/completions", Models: "models"})
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			api := &APIConfig{Context: ctx}
			history := []Message{{Role: "user", Content: "q"}}
			sentinel := errors.New("sender failed")
			done := false
			var err error
			if mode == "cancel streaming" || mode == "sender failure" {
				err = driver.ChatStreamlyWithMessages("m", history, api, nil, func(c, r *string) error {
					if c != nil && *c == "[DONE]" {
						done = true
					}
					if mode == "sender failure" {
						return sentinel
					}
					cancel()
					return nil
				})
			} else {
				go func() { <-started; cancel() }()
				if mode == "cancel normal" {
					_, err = driver.ChatWithMessages("m", api, history, nil)
				} else {
					_, err = driver.ListModels(api)
				}
			}
			want := error(context.Canceled)
			if mode == "sender failure" {
				want = sentinel
			}
			if !errors.Is(err, want) || done {
				t.Fatal(err, done)
			}
			select {
			case <-stopped:
			case <-time.After(time.Second):
				t.Fatal("HTTP response not closed/canceled")
			}
			t.Log("HTTP cancellation/body cleanup verified; no success terminal")
		})
	}
	for _, terminal := range []string{"data: [DONE]\n\n", "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n"} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\n"+terminal)
		}))
		driver := NewLMStudioModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat/completions"})
		ctx, cancel := context.WithCancel(context.Background())
		done := false
		err := driver.ChatStreamlyWithMessages("m", []Message{{Role: "user", Content: "q"}}, &APIConfig{Context: ctx}, nil, func(c, r *string) error {
			if c != nil && *c == "[DONE]" {
				done = true
			}
			cancel()
			return nil
		})
		cancel()
		server.Close()
		if !errors.Is(err, context.Canceled) || done {
			t.Fatal("buffered terminal after cancellation", err)
		}
	}
}

func TestLMStudioInputIsolationAndUnsupported(t *testing.T) {
	urls := map[string]string{"default": "http://original.invalid/v1"}
	driver := NewLMStudioModel(urls, URLSuffix{Chat: "chat/completions", Models: "models"})
	urls["default"] = "http://mutated.invalid"
	if driver.BaseURL["default"] != "http://original.invalid/v1" {
		t.Fatal("constructor retained caller map")
	}
	name, q := "m", "q"
	for _, call := range []func() error{
		func() error { _, e := NewRerankModel(driver, &name, nil).Rerank(q, []string{q}); return e },
		func() error { _, e := driver.Balance(nil); return e },
		func() error { return driver.CheckConnection(nil) },
		func() error { _, e := driver.ChatStreamly(&name, nil, &q, nil); return e },
		func() error { return driver.ChatStreamlyWithChannel(&name, nil, &q, nil, nil) },
	} {
		if err := call(); err == nil {
			t.Fatal("unsupported capability succeeded")
		}
	}
	for _, test := range []struct {
		name    string
		history []Message
	}{
		{"", []Message{{Role: "user", Content: q}}}, {name, nil}, {name, []Message{{Role: "invalid", Content: q}}}, {name, []Message{{Role: "user", Content: ""}}}, {name, []Message{{Role: "user", Content: 42}}},
	} {
		if _, err := driver.ChatWithMessages(test.name, nil, test.history, nil); err == nil {
			t.Fatal("bad input accepted")
		}
	}
	if err := driver.ChatStreamlyWithMessages(name, []Message{{Role: "user", Content: q}}, nil, nil, nil); err == nil {
		t.Fatal("nil sender accepted")
	}
	if err := driver.ChatStreamlyWithSender(nil, &q, nil, nil, func(*string, *string) error { return nil }); err == nil {
		t.Fatal("nil model accepted")
	}
	region := "missing"
	if _, err := driver.ListModels(&APIConfig{Region: &region}); err == nil {
		t.Fatal("unknown region accepted")
	}
	if _, err := NewLMStudioModel(nil, URLSuffix{Models: "models"}).ListModels(nil); err == nil {
		t.Fatal("missing URL accepted")
	}
	if _, err := NewLMStudioModel(urls, URLSuffix{}).ListModels(nil); err == nil {
		t.Fatal("missing suffix accepted")
	}
}

// Observe per-call contexts without waiting for the 120 second deadline.
type lmStudioTransportFunc func(*http.Request) (*http.Response, error)

func (f lmStudioTransportFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestLMStudioPerCallTimeouts(t *testing.T) {
	driver := NewLMStudioModel(map[string]string{"default": "http://fixture.invalid/v1"}, URLSuffix{Chat: "chat/completions", Models: "models"})
	stream := false
	var contexts []context.Context
	driver.httpClient.Transport = lmStudioTransportFunc(func(r *http.Request) (*http.Response, error) {
		contexts = append(contexts, r.Context())
		deadline, exists := r.Context().Deadline()
		want := nonStreamCallTimeout
		if stream {
			want = streamCallTimeout
		}
		if !exists || time.Until(deadline) > want || time.Until(deadline) < want-time.Second {
			t.Errorf("operation deadline = %v, want %v", time.Until(deadline), want)
		}
		body := `{"choices":[{"message":{"content":"answer"}}]}`
		if r.Method == "GET" {
			body = `{"data":[]}`
		}
		if stream {
			body = "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n"
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header)}, nil
	})
	history := []Message{{Role: "user", Content: "q"}}
	if _, err := driver.ChatWithMessages("m", nil, history, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := driver.ListModels(nil); err != nil {
		t.Fatal(err)
	}
	for _, ctx := range contexts {
		if !errors.Is(ctx.Err(), context.Canceled) {
			t.Fatal("per-call context not released")
		}
	}
	stream = true
	if err := driver.ChatStreamlyWithMessages("m", history, nil, nil, func(*string, *string) error { return nil }); err != nil {
		t.Fatal(err)
	}
}

func TestLMStudioSenderErrorsAndScannerLimit(t *testing.T) {
	sentinel := errors.New("delivery failed")
	for _, mode := range []string{"reasoning", "terminal", "scanner limit"} {
		t.Run(mode, func(t *testing.T) {
			payload := "data: {\"choices\":[{\"delta\":{\"content\":\"answer\",\"reasoning_content\":\"reason\"}}]}\n\ndata: [DONE]\n\n"
			if mode == "scanner limit" {
				payload = "data: " + strings.Repeat("a", 4*1024*1024) + "\n\ndata: [DONE]\n\n"
			}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, payload) }))
			defer server.Close()
			driver := NewLMStudioModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat/completions"})
			var frames []string
			err := driver.ChatStreamlyWithMessages("m", []Message{{Role: "user", Content: "q"}}, nil, nil, func(c, r *string) error {
				if mode == "reasoning" && r != nil {
					return sentinel
				}
				if mode == "terminal" && c != nil && *c == "[DONE]" {
					return sentinel
				}
				if c != nil {
					frames = append(frames, *c)
				}
				return nil
			})
			if err == nil {
				t.Fatal("stream failure succeeded")
			}
			if mode != "scanner limit" && !errors.Is(err, sentinel) {
				t.Fatal(err)
			}
			for _, frame := range frames {
				if frame == "[DONE]" {
					t.Fatal("successful terminal after failure")
				}
			}
			if mode == "reasoning" && len(frames) > 0 {
				t.Fatal("content delivered after reasoning sender failed")
			}
		})
	}
}
