package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func jieKouAIFixture(t *testing.T, handler http.HandlerFunc) *JieKouAIModel {
	t.Helper()
	srv := providerTestServer(t, handler)
	return NewJieKouAIModel(map[string]string{"default": srv.URL + "/v1/"}, URLSuffix{Chat: " /chat/ ", Models: " /models/ ", Embedding: " /embed/ ", Rerank: " /rerank/ "})
}

func TestAfterB7JieKouAIWire(t *testing.T) {
	m := jieKouAIFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer key" {
			t.Error("auth", r.Header)
		}
		if r.Method == http.MethodGet {
			raw, _ := io.ReadAll(r.Body)
			if r.URL.Path != "/v1/models" || len(raw) != 0 || r.Header.Get("Accept") != "application/json" {
				t.Error("discovery wire", r.URL, r.Header, string(raw))
			}
			fmt.Fprint(w, `{"data":[]}`)
			return
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		if body["model"] != "model" || r.Header.Get("Content-Type") != "application/json" {
			t.Error("model/header", body, r.Header)
		}
		switch r.URL.Path {
		case "/v1/chat":
			if body["temperature"] != float64(0) || body["enable_thinking"] != false {
				t.Error("zero config", body)
			}
			if body["stream"] == true {
				if r.Header.Get("Accept") != "text/event-stream" || body["do_sample"] != false {
					t.Error("stream wire", body, r.Header)
				}
				fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"reason\"}}]}\ndata: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\ndata: [DONE]\n")
			} else {
				if r.Header.Get("Accept") != "application/json" {
					t.Error("JSON Accept", r.Header)
				}
				fmt.Fprint(w, `{"choices":[{"message":{"content":"answer","reasoning_content":"reason"}}]}`)
			}
		case "/v1/embed":
			fmt.Fprint(w, `{"data":[{"index":1,"embedding":[1,0]},{"index":0,"embedding":[0,0]}]}`)
		case "/v1/rerank":
			if body["query"] != "query" || body["top_n"] != float64(2) {
				t.Error("rerank body", body)
			}
			fmt.Fprint(w, `{"results":[{"index":1,"relevance_score":0},{"index":0,"relevance_score":0.9}]}`)
		default:
			t.Error("unexpected path", r.URL.Path)
			w.WriteHeader(404)
		}
	})
	api := &APIConfig{APIKey: stringPtr(" key ")}
	on, off, zero := true, false, 0.0
	cfg := &ChatConfig{Stream: &on, Thinking: &off, DoSample: &off, Temperature: &zero}
	if got, err := m.ChatWithMessages(" model ", api, []Message{{Role: "user", Content: "q"}}, cfg); err != nil || *got.Answer != "answer" {
		t.Fatal(got, err)
	}
	cfg.Stream = &off
	var answer, reason string
	err := m.ChatStreamlyWithMessages(" model ", []Message{{Role: "user", Content: "q"}}, api, cfg, func(c, r *string) error {
		if c != nil {
			answer += *c
		}
		if r != nil {
			reason += *r
		}
		return nil
	})
	if err != nil || answer != "answer[DONE]" || reason != "reason" {
		t.Fatal(answer, reason, err)
	}
	if names, err := m.ListModels(api); err != nil || names == nil || len(names) != 0 {
		t.Fatal(names, err)
	}
	name := " model "
	if got, err := m.Embed(&name, []string{"one", "two"}, api, nil); err != nil || len(got) != 2 || got[0].Index != 0 || got[0].Embedding[0] != 0 {
		t.Fatal(got, err)
	}
	if got, err := m.Rerank(&name, " query ", []string{"one", "two"}, api, nil); err != nil || len(got.Data) != 2 || got.Data[1].RelevanceScore != 0 {
		t.Fatal(got, err)
	}
	if name != " model " || *cfg.Stream {
		t.Fatal("caller inputs mutated")
	}
}

func TestAfterB7JieKouAIDenial(t *testing.T) {
	var calls atomic.Int32
	m := jieKouAIFixture(t, func(w http.ResponseWriter, r *http.Request) { calls.Add(1) })
	api := &APIConfig{APIKey: stringPtr("key")}
	msg := []Message{{Role: "user", Content: "q"}}
	send := func(*string, *string) error { return nil }
	cases := []func() error{
		func() error { _, e := m.ChatWithMessages(" ", api, msg, nil); return e },
		func() error { _, e := m.ChatWithMessages("model", nil, msg, nil); return e },
		func() error {
			_, e := m.ChatWithMessages("model", &APIConfig{APIKey: stringPtr(" ")}, msg, nil)
			return e
		},
		func() error { _, e := m.ChatWithMessages("model", api, nil, nil); return e },
		func() error { return m.ChatStreamlyWithMessages("model", msg, api, nil, nil) },
		func() error { return m.ChatStreamlyWithMessages("model", msg, nil, nil, send) },
		func() error { _, e := m.Embed(nil, []string{"q"}, api, nil); return e },
		func() error { _, e := m.Embed(stringPtr("model"), []string{"q"}, nil, nil); return e },
		func() error { _, e := m.Rerank(stringPtr("model"), " ", []string{"q"}, api, nil); return e },
		func() error { _, e := m.Rerank(nil, "q", []string{"q"}, api, nil); return e },
		func() error {
			_, e := m.Rerank(stringPtr("model"), "q", []string{"q"}, api, &RerankConfig{TopN: -1})
			return e
		},
		func() error { _, e := m.ListModels(nil); return e },
	}
	for i, call := range cases {
		if err := call(); err == nil {
			t.Error("invalid input accepted", i)
		}
	}
	if calls.Load() != 0 {
		t.Fatal("denial reached HTTP", calls.Load())
	}
}

func TestAfterB7JieKouAIRawFailures(t *testing.T) {
	for kind, rows := range map[string][]string{
		"models": {`{}`, `{"data":null}`, `{"data":[null]}`, `{"data":[{"id":" "}]}`, `{"data":[{"id":"x"},{"id":"x"}]}`, `{"data":[]} {}`},
		"embed":  {`{}`, `{"data":[{"embedding":[0]}]}`, `{"data":[{"index":0,"embedding":[null]}]}`, `{"data":[{"index":0,"embedding":[1e999]}]}`, `{"data":[{"index":2,"embedding":[0]}]}`, `{"data":[{"index":0,"embedding":[0]},{"index":0,"embedding":[1]}]}`},
		"rerank": {`{}`, `{"results":[{"relevance_score":0}]}`, `{"results":[{"index":0,"relevance_score":null}]}`, `{"results":[{"index":0,"relevance_score":1e999}]}`, `{"results":[{"index":2,"relevance_score":0}]}`, `{"results":[{"index":0,"relevance_score":0},{"index":0,"relevance_score":1}]}`},
	} {
		for i, raw := range rows {
			t.Run(fmt.Sprintf("%s/%d", kind, i), func(t *testing.T) {
				m := jieKouAIFixture(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, raw) })
				api := &APIConfig{APIKey: stringPtr("key")}
				name := "model"
				texts := []string{"one"}
				if strings.Contains(raw, `},{`) {
					texts = append(texts, "two")
				}
				var err error
				switch kind {
				case "models":
					_, err = m.ListModels(api)
				case "embed":
					_, err = m.Embed(&name, texts, api, nil)
				case "rerank":
					_, err = m.Rerank(&name, "q", texts, api, nil)
				}
				if err == nil {
					t.Fatal("malformed response accepted", raw)
				}
			})
		}
	}
}

func TestAfterB7JieKouAIStreamFailure(t *testing.T) {
	for _, tail := range []string{"data: {bad}\n", "data: {\"choices\":[null]}\n", "data: {\"choices\":[{\"finish_reason\":\"content_filter\"}]}\n", ""} {
		t.Run(tail, func(t *testing.T) {
			m := jieKouAIFixture(t, func(w http.ResponseWriter, r *http.Request) {
				fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"prefix\"}}]}\n"+tail)
			})
			var answer string
			err := m.ChatStreamlyWithMessages("model", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: stringPtr("key")}, nil, func(c, r *string) error {
				if c != nil {
					answer += *c
				}
				return nil
			})
			if err == nil || answer != "prefix" {
				t.Fatal("failure after prefix lost", answer, err)
			}
		})
	}
}

func TestAfterB7JieKouAICancellation(t *testing.T) {
	for _, kind := range []string{"chat", "stream", "embed", "rerank", "models"} {
		t.Run(kind, func(t *testing.T) {
			m := jieKouAIFixture(t, func(w http.ResponseWriter, r *http.Request) {
				io.Copy(io.Discard, r.Body)
				w.WriteHeader(200)
				w.(http.Flusher).Flush()
				<-r.Context().Done()
			})
			ctx, cancel := context.WithTimeout(t.Context(), 50*time.Millisecond)
			defer cancel()
			api := &APIConfig{APIKey: stringPtr("key"), Context: ctx}
			name := "model"
			msg := []Message{{Role: "user", Content: "q"}}
			var err error
			switch kind {
			case "chat":
				_, err = m.ChatWithMessages(name, api, msg, nil)
			case "stream":
				err = m.ChatStreamlyWithMessages(name, msg, api, nil, func(*string, *string) error { return nil })
			case "embed":
				_, err = m.Embed(&name, []string{"q"}, api, nil)
			case "rerank":
				_, err = m.Rerank(&name, "q", []string{"q"}, api, nil)
			case "models":
				_, err = m.ListModels(api)
			}
			if !errors.Is(err, context.DeadlineExceeded) {
				t.Fatal("deadline lost", err)
			}
		})
	}
}

func TestAfterB7JieKouAILargeStreamAndStop(t *testing.T) {
	for _, mode := range []string{"large", "sender-error", "cancel", "idle"} {
		t.Run(mode, func(t *testing.T) {
			closed := make(chan struct{})
			prefix := strings.Repeat("x", 80*1024)
			m := jieKouAIFixture(t, func(w http.ResponseWriter, r *http.Request) {
				defer close(closed)
				io.Copy(io.Discard, r.Body)
				if mode == "idle" {
					w.WriteHeader(200)
					w.(http.Flusher).Flush()
					<-r.Context().Done()
					return
				}
				payload, _ := json.Marshal(map[string]any{"choices": []any{map[string]any{"delta": map[string]any{"content": prefix}}}})
				fmt.Fprintf(w, "data: %s\n", payload)
				w.(http.Flusher).Flush()
				if mode == "large" {
					fmt.Fprint(w, "data: [DONE]\n")
					return
				}
				<-r.Context().Done()
			})
			if mode == "idle" {
				m.streamIdleTimeout = 25 * time.Millisecond
			}
			ctx, cancel := context.WithTimeout(t.Context(), time.Second)
			defer cancel()
			stop := errors.New("sender stopped")
			var answer string
			err := m.ChatStreamlyWithMessages("model", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil, func(c, r *string) error {
				if c != nil {
					answer += *c
				}
				if mode == "sender-error" {
					return stop
				}
				if mode == "cancel" {
					cancel()
				}
				return nil
			})
			switch mode {
			case "large":
				if err != nil || answer != prefix+"[DONE]" {
					t.Fatal("large stream", len(answer), err)
				}
			case "sender-error":
				if !errors.Is(err, stop) || answer != prefix {
					t.Fatal("sender error", err)
				}
			case "cancel":
				if !errors.Is(err, context.Canceled) || answer != prefix {
					t.Fatal("cancel", err)
				}
			case "idle":
				if !errors.Is(err, context.DeadlineExceeded) || answer != "" {
					t.Fatal("idle timeout", err)
				}
			}
			select {
			case <-closed:
			case <-time.After(time.Second):
				t.Fatal("response body/socket did not close")
			}
		})
	}
}

func TestAfterB7JieKouAIHTTPFailures(t *testing.T) {
	for _, kind := range []string{"chat", "stream", "embed", "rerank", "models"} {
		t.Run(kind, func(t *testing.T) {
			m := jieKouAIFixture(t, func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(http.StatusUnauthorized)
				fmt.Fprint(w, "secret-provider-response")
			})
			api := &APIConfig{APIKey: stringPtr("key")}
			name := "model"
			msg := []Message{{Role: "user", Content: "q"}}
			var err error
			switch kind {
			case "chat":
				_, err = m.ChatWithMessages(name, api, msg, nil)
			case "stream":
				err = m.ChatStreamlyWithMessages(name, msg, api, nil, func(*string, *string) error { return nil })
			case "embed":
				_, err = m.Embed(&name, []string{"q"}, api, nil)
			case "rerank":
				_, err = m.Rerank(&name, "q", []string{"q"}, api, nil)
			case "models":
				_, err = m.ListModels(api)
			}
			if err == nil || strings.Contains(err.Error(), "secret-provider-response") {
				t.Fatal("HTTP failure lost/leaked", err)
			}
		})
	}
}
