package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func xiaomiFixture(t *testing.T, handler http.HandlerFunc) *XiaomiModel {
	t.Helper()
	srv := providerTestServer(t, handler)
	return NewXiaomiModel(map[string]string{"default": srv.URL}, URLSuffix{Chat: "v1/chat/completions"})
}

func TestXiaomiFactoryAndWire(t *testing.T) {
	key, name := "controlled-key", "mimo-v2.5"
	zero, maxTokens, thinking := 0.0, 128, false
	messages := []Message{
		{Role: "system", Content: "system"},
		{Role: "assistant", Content: "prior"},
		{Role: "user", Content: []map[string]any{
			{"type": "text", "text": "describe"},
			{"type": "image_url", "image_url": map[string]any{"url": "https://files.invalid/image.png"}},
		}},
	}
	stop := []string{"END"}
	config := &ChatConfig{MaxTokens: &maxTokens, Temperature: &zero, TopP: &zero, Stop: &stop, Thinking: &thinking}
	var calls atomic.Int32
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/v1/chat/completions" || r.Header.Get("api-key") != key || r.Header.Get("Authorization") != "" || r.Header.Get("Content-Type") != "application/json" {
			t.Error("incorrect Xiaomi path or headers", r.URL, r.Header)
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		if body["model"] != name || body["stream"] != false || body["max_completion_tokens"] != float64(maxTokens) || body["max_tokens"] != nil || body["temperature"] != zero || body["top_p"] != zero || body["enable_thinking"] != nil || body["thinking"].(map[string]any)["type"] != "disabled" {
			t.Error("incorrect Xiaomi body", body)
		}
		encodedMessages, _ := json.Marshal(messages)
		gotMessages, _ := json.Marshal(body["messages"])
		var want, got any
		_ = json.Unmarshal(encodedMessages, &want)
		_ = json.Unmarshal(gotMessages, &got)
		if !reflect.DeepEqual(want, got) || !reflect.DeepEqual(body["stop"], []any{"END"}) {
			t.Error("history, multimodal content or stop was lost", body)
		}
		fmt.Fprint(w, `{"choices":[{"message":{"content":"answer","reasoning_content":"reason"}}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}`)
	}))
	driver, err := NewModelFactory().CreateModelDriver("Xiaomi", map[string]string{"default": srv.URL + "/v1/"}, URLSuffix{Chat: "/v1/chat/completions/"})
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := driver.(*XiaomiModel); !ok || driver.Name() != "xiaomi" {
		t.Fatalf("factory selected %T (%s)", driver, driver.Name())
	}
	response, err := driver.ChatWithMessages(name, &APIConfig{APIKey: &key}, messages, config)
	if err != nil || response == nil || response.Answer == nil || *response.Answer != "answer" || response.ReasoningContent == nil || *response.ReasoningContent != "reason" {
		t.Fatal(response, err)
	}
	if calls.Load() != 1 || *config.Thinking || *config.MaxTokens != maxTokens || *config.Temperature != zero {
		t.Fatal("request count or caller input changed")
	}
}

func TestXiaomiInstanceAndRegionIsolation(t *testing.T) {
	for _, endpoint := range []string{"first", "second"} {
		t.Run(endpoint, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/custom/chat" || r.Header.Get("api-key") != endpoint {
					t.Error("instance route or key crossed", r.URL, r.Header)
				}
				fmt.Fprintf(w, `{"choices":[{"message":{"content":%q}}]}`, endpoint)
			}))
			urls := map[string]string{"default": "http://unused.invalid"}
			prototype := NewXiaomiModel(urls, URLSuffix{Chat: "custom/chat"})
			urls["default"] = srv.URL
			if prototype.BaseURL["default"] != "http://unused.invalid" {
				t.Fatal("constructor retained caller map")
			}
			instanceURLs := map[string]string{"private": srv.URL}
			instance := prototype.NewInstance(instanceURLs)
			instanceURLs["private"] = "http://unused.invalid"
			region, key := "private", endpoint
			response, err := instance.ChatWithMessages("mimo", &APIConfig{APIKey: &key, Region: &region}, []Message{{Role: "user", Content: "q"}}, nil)
			if err != nil || response.Answer == nil || *response.Answer != endpoint {
				t.Fatal(response, err)
			}
			unknown := "unconfigured"
			if _, err := instance.ChatWithMessages("mimo", &APIConfig{APIKey: &key, Region: &unknown}, []Message{{Role: "user", Content: "q"}}, nil); err == nil {
				t.Fatal("unknown region silently selected another endpoint")
			}
		})
	}
}

func TestXiaomiStreamEventsAndUsage(t *testing.T) {
	for label, terminal := range map[string]string{"done": "data: [DONE]\r\n\r\n", "finish": "data: {\"choices\":[{\"finish_reason\":\"stop\"}]}\r\n\r\n"} {
		t.Run(label, func(t *testing.T) {
			model := xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if r.Header.Get("Accept") != "text/event-stream" || body["stream"] != true || body["thinking"].(map[string]any)["type"] != "enabled" {
					t.Error("stream request contract", body, r.Header)
				}
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprint(w, ": keepalive\r\nevent: message\r\ndata: {\"choices\":[{\"delta\":\r\ndata: {\"reasoning_content\":\"reason\"}}]}\r\n\r\n")
				fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\r\n\r\n")
				fmt.Fprint(w, "data: {\"choices\":[],\"usage\":{\"total_tokens\":5}}\r\n\r\n", terminal)
			})
			key, on := "key", true
			var answer, reasoning string
			err := model.ChatStreamlyWithMessages("mimo", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: &key}, &ChatConfig{Thinking: &on}, func(c, r *string) error {
				if c != nil {
					answer += *c
				}
				if r != nil {
					reasoning += *r
				}
				return nil
			})
			if err != nil || answer != "answer[DONE]" || reasoning != "reason" {
				t.Fatal(answer, reasoning, err)
			}
		})
	}
}

func TestXiaomiLongStreamAndEmptyRegionMap(t *testing.T) {
	text := strings.Repeat("x", 1024*1024+128)
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/chat/completions" {
			t.Error("default endpoint", r.URL)
		}
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":%q}}]}\n\ndata: [DONE]\n\n", text)
	}))
	model := NewXiaomiModel(map[string]string{"": srv.URL}, URLSuffix{})
	name, key, message := "mimo", "key", "q"
	var answer string
	err := model.ChatStreamlyWithSender(&name, &message, &APIConfig{APIKey: &key}, nil, func(c, r *string) error {
		if c != nil {
			answer += *c
		}
		return nil
	})
	if err != nil || answer != text+"[DONE]" {
		t.Fatal("large stream or empty-region endpoint failed", len(answer), err)
	}
}

func TestXiaomiDenialBeforeHTTP(t *testing.T) {
	var calls atomic.Int32
	model := xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) { calls.Add(1) })
	key, off := "key", false
	api := &APIConfig{APIKey: &key}
	messages := []Message{{Role: "user", Content: "q"}}
	sender := func(*string, *string) error { return nil }
	for _, run := range []func() error{
		func() error { _, err := model.ChatWithMessages("", api, messages, nil); return err },
		func() error { _, err := model.ChatWithMessages("mimo", nil, messages, nil); return err },
		func() error { _, err := model.ChatWithMessages("mimo", api, nil, nil); return err },
		func() error { return model.ChatStreamlyWithMessages("mimo", messages, api, nil, nil) },
		func() error {
			return model.ChatStreamlyWithMessages("mimo", messages, api, &ChatConfig{Stream: &off}, sender)
		},
	} {
		if err := run(); err == nil {
			t.Fatal("invalid request succeeded")
		}
	}
	for _, value := range []float64{math.NaN(), math.Inf(1), math.Inf(-1)} {
		if _, err := model.ChatWithMessages("mimo", api, messages, &ChatConfig{Temperature: &value}); err == nil {
			t.Fatal("nonfinite generation value accepted")
		}
	}
	if calls.Load() != 0 {
		t.Fatalf("invalid calls reached HTTP: %d", calls.Load())
	}
}

func TestXiaomiResponseFailures(t *testing.T) {
	for label, body := range map[string]string{
		"provider-error": `{"error":{"message":"private-secret"}}`,
		"missing-answer": `{"choices":[]}`,
		"null-answer":    `{"choices":[{"message":{"content":null}}]}`,
		"malformed":      `{"choices":[`,
		"trailing":       `{"choices":[{"message":{"content":"answer"}}]} {}`,
		"tools":          `{"choices":[{"message":{"content":null,"tool_calls":[{"id":"call"}]},"finish_reason":"tool_calls"}]}`,
		"filtered":       `{"choices":[{"message":{"content":"answer"},"finish_reason":"content_filter"}]}`,
		"oversized":      strings.Repeat(" ", MaxMediaBytes+1),
	} {
		t.Run(label, func(t *testing.T) {
			model := xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) })
			key := "key"
			if response, err := model.ChatWithMessages("mimo", &APIConfig{APIKey: &key}, []Message{{Role: "user", Content: "q"}}, nil); err == nil || response != nil || strings.Contains(err.Error(), "private-secret") {
				t.Fatal(response, err)
			}
		})
	}
	for _, status := range []int{401, 429, 500} {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			model := xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(status)
				fmt.Fprint(w, "private-secret")
			})
			key := "key"
			if _, err := model.ChatWithMessages("mimo", &APIConfig{APIKey: &key}, []Message{{Role: "user", Content: "q"}}, nil); err == nil || !strings.Contains(err.Error(), fmt.Sprint(status)) || strings.Contains(err.Error(), "private-secret") {
				t.Fatal(err)
			}
		})
	}
}

func TestXiaomiStreamFailures(t *testing.T) {
	answer := "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\n"
	for label, body := range map[string]string{
		"malformed":        "data: {bad}\n\n",
		"provider-error":   "data: {\"error\":{\"message\":\"private-secret\"}}\n\n",
		"terminal-only":    "data: [DONE]\n\n",
		"truncated":        answer,
		"incomplete-event": "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}",
		"tools":            answer + "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"id\":\"call\"}]},\"finish_reason\":\"tool_calls\"}]}\n\n",
		"filtered":         answer + "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"content_filter\"}]}\n\n",
		"oversized-line":   "data: " + strings.Repeat(" ", 4*1024*1024+1) + "\n\n",
		"oversized-event":  strings.Repeat("data: "+strings.Repeat(" ", 1024*1024)+"\n", 5) + "\n",
	} {
		t.Run(label, func(t *testing.T) {
			model := xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprint(w, body)
			})
			key, done := "key", false
			err := model.ChatStreamlyWithMessages("mimo", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: &key}, nil, func(c, r *string) error {
				done = done || c != nil && *c == "[DONE]"
				return nil
			})
			if err == nil || done || strings.Contains(err.Error(), "private-secret") {
				t.Fatal(done, err)
			}
		})
	}
}

func TestXiaomiCancellationAndSenderDrain(t *testing.T) {
	for _, mode := range []string{"nonstream", "cancel-stream", "sender-error", "idle"} {
		t.Run(mode, func(t *testing.T) {
			started, closed := make(chan struct{}), make(chan struct{})
			model := xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) {
				_, _ = io.Copy(io.Discard, r.Body)
				if mode != "nonstream" {
					w.Header().Set("Content-Type", "text/event-stream")
					if mode == "sender-error" {
						fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\n")
					}
					w.(http.Flusher).Flush()
				}
				close(started)
				select {
				case <-r.Context().Done():
					close(closed)
				case <-time.After(3 * time.Second):
					t.Error("request socket was not closed")
				}
			})
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			key, sentinel := "key", errors.New("sender stopped")
			api := &APIConfig{APIKey: &key, Context: ctx}
			result := make(chan error, 1)
			if mode == "idle" {
				model.streamIdleTimeout = 30 * time.Millisecond
			}
			go func() {
				if mode == "nonstream" {
					_, err := model.ChatWithMessages("mimo", api, []Message{{Role: "user", Content: "q"}}, nil)
					result <- err
					return
				}
				result <- model.ChatStreamlyWithMessages("mimo", []Message{{Role: "user", Content: "q"}}, api, nil, func(c, r *string) error {
					if mode == "sender-error" {
						return sentinel
					}
					return nil
				})
			}()
			select {
			case <-started:
			case <-time.After(time.Second):
				t.Fatal("request did not start")
			}
			if mode == "nonstream" || mode == "cancel-stream" {
				cancel()
			}
			select {
			case err := <-result:
				if err == nil || mode == "sender-error" && !errors.Is(err, sentinel) || mode == "idle" && !errors.Is(err, context.DeadlineExceeded) || (mode == "nonstream" || mode == "cancel-stream") && !errors.Is(err, context.Canceled) {
					t.Fatal(err)
				}
			case <-time.After(time.Second):
				t.Fatal("model call did not drain")
			}
			select {
			case <-closed:
			case <-time.After(time.Second):
				t.Fatal("server did not observe cancellation")
			}
		})
	}
}

func TestXiaomiRejectsRedirectAndUnsupportedCapabilities(t *testing.T) {
	var targetCalls atomic.Int32
	target := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { targetCalls.Add(1) }))
	model := xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusTemporaryRedirect)
	})
	name, key := "mimo", "key"
	api := &APIConfig{APIKey: &key}
	if _, err := model.ChatWithMessages(name, api, []Message{{Role: "user", Content: "q"}}, nil); err == nil || targetCalls.Load() != 0 {
		t.Fatal("redirect followed", err, targetCalls.Load())
	}
	for _, run := range []func() error{
		func() error { _, err := model.Embed(&name, []string{"q"}, api, nil); return err },
		func() error { _, err := model.Rerank(&name, "q", []string{"d"}, api, nil); return err },
		func() error { _, err := model.ListModels(api); return err },
		func() error { _, err := model.Balance(api); return err },
		func() error { return model.CheckConnection(api) },
	} {
		if err := run(); !errors.Is(err, ErrUnsupportedCapability) {
			t.Fatal("capability did not fail explicitly", err)
		}
	}
}
