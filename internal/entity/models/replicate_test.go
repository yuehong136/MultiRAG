package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func newReplicateTestModel(base string) *ReplicateModel {
	r := NewReplicateModel(map[string]string{"default": base}, URLSuffix{Chat: "v1/predictions", Models: "v1/models"})
	r.pollInterval = time.Millisecond
	return r
}

func TestReplicatePredictionRoutesAndPrompt(t *testing.T) {
	version := strings.Repeat("a", 64)
	for _, name := range []string{"owner/model", version, "owner/model:" + version} {
		t.Run(name, func(t *testing.T) {
			key := "controlled-key"
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				path := "/v1/predictions"
				if name == "owner/model" {
					path = "/v1/models/owner/model/predictions"
				}
				if r.URL.Path != path || r.Header.Get("Authorization") != "Bearer "+key || r.Header.Get("Prefer") != "wait=60" {
					t.Error("incorrect prediction request", r.URL.Path)
				}
				var body struct {
					Input   map[string]any `json:"input"`
					Version string         `json:"version"`
					Stream  bool           `json:"stream"`
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if name != "owner/model" && body.Version != version || name == "owner/model" && body.Version != "" || body.Stream {
					t.Error("incorrect version or stream", body)
				}
				if body.Input["system_prompt"] != "system" || body.Input["prompt"] != "user: hello\nassistant: hi\nuser: again" || body.Input["max_new_tokens"] != float64(12) || body.Input["temperature"] != float64(0) || body.Input["stop"] != nil {
					t.Error("prompt/config lost", body.Input)
				}
				fmt.Fprint(w, `{"id":"pred-1","status":"succeeded","output":["hello"," world"]}`)
			}))
			defer srv.Close()
			driver, err := NewModelFactory().CreateModelDriver("Replicate", map[string]string{"default": srv.URL}, URLSuffix{Chat: "v1/predictions", Models: "v1/models"})
			if err != nil || driver.Name() != "replicate" {
				t.Fatal(driver, err)
			}
			maxTokens, temperature, stop := 12, 0.0, []string{"stop"}
			resp, err := driver.ChatWithMessages(name, &APIConfig{APIKey: &key}, []Message{{Role: "system", Content: "system"}, {Role: "user", Content: "hello"}, {Role: "assistant", Content: "hi"}, {Role: "user", Content: "again"}}, &ChatConfig{MaxTokens: &maxTokens, Temperature: &temperature, Stop: &stop})
			if err != nil || resp.Answer == nil || *resp.Answer != "hello world" {
				t.Fatal(resp, err)
			}
		})
	}
}

func TestReplicatePollingAndFallbackTerminalState(t *testing.T) {
	for _, status := range []string{"succeeded", "failed", "canceled", "successful", "unknown"} {
		for _, stream := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s-stream=%v", status, stream), func(t *testing.T) {
				var polls atomic.Int64
				key := "key"
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.Method == http.MethodPost {
						fmt.Fprintf(w, `{"id":"pred-1","status":"starting","urls":{"get":"http://%s/v1/predictions/pred-1"}}`, r.Host)
					} else {
						polls.Add(1)
						if r.ContentLength != 0 || r.Header.Get("Authorization") != "Bearer key" {
							t.Error("poll request invalid")
						}
						fmt.Fprintf(w, `{"id":"pred-1","status":%q,"output":"partial"}`, status)
					}
				}))
				defer srv.Close()
				m := newReplicateTestModel(srv.URL)
				var chunks []string
				var err error
				if stream {
					err = m.ChatStreamlyWithMessages("owner/model", []Message{{Role: "user", Content: "hello"}}, &APIConfig{APIKey: &key}, nil, func(content, _ *string) error { chunks = append(chunks, *content); return nil })
				} else {
					_, err = m.ChatWithMessages("owner/model", &APIConfig{APIKey: &key}, []Message{{Role: "user", Content: "hello"}}, nil)
				}
				if status == "succeeded" {
					if err != nil || stream && strings.Join(chunks, "") != "partial[DONE]" {
						t.Fatal(chunks, err)
					}
				} else if err == nil || len(chunks) != 0 {
					t.Fatal("failed/unknown prediction emitted success", chunks, err)
				}
				if polls.Load() != 1 {
					t.Fatal("wrong poll count", polls.Load())
				}
			})
		}
	}
}

func TestReplicateSSETerminalContract(t *testing.T) {
	for _, tc := range []struct {
		name, events string
		valid        bool
	}{
		{"success", "event: output\ndata: hello\ndata: world\n\nevent: done\ndata: {}\n\n", true},
		{"large", "event: output\ndata: " + strings.Repeat("a", 1024*1024+5) + "\n\nevent: done\ndata: {}\n\n", true},
		{"cancelled", "event: output\ndata: partial\n\nevent: done\ndata: {\"reason\":\"canceled\"}\n\n", false},
		{"error", "event: error\ndata: {\"detail\":\"private-provider-body\"}\n\nevent: done\ndata: {\"reason\":\"error\"}\n\n", false},
		{"bad-done", "event: output\ndata: partial\n\nevent: done\ndata: invalid\n\n", false},
		{"empty", "event: done\ndata: {}\n\n", false},
		{"truncated", "event: output\ndata: partial\n\nevent: done\ndata: {}", false},
		{"missing-done", "event: output\ndata: partial\n\n", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			key := "key"
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method == http.MethodPost {
					fmt.Fprintf(w, `{"id":"pred-1","status":"processing","urls":{"stream":"http://%s/stream"}}`, r.Host)
					return
				}
				if r.Header.Get("Accept") != "text/event-stream" {
					t.Error("missing SSE accept header")
				}
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprint(w, tc.events)
			}))
			defer srv.Close()
			var chunks []string
			err := newReplicateTestModel(srv.URL).ChatStreamlyWithMessages("owner/model", []Message{{Role: "user", Content: "hello"}}, &APIConfig{APIKey: &key}, nil, func(content, _ *string) error { chunks = append(chunks, *content); return nil })
			done := len(chunks) > 0 && chunks[len(chunks)-1] == "[DONE]"
			if tc.valid != (err == nil && done) || !tc.valid && (err == nil || done) || err != nil && strings.Contains(err.Error(), "private-provider-body") {
				t.Fatal("wrong SSE completion", done, err)
			}
		})
	}
}

func TestReplicateInvalidResponsesAndInputs(t *testing.T) {
	key := "key"
	for _, response := range []string{`{}`, `{"id":"pred-1","status":"successful","output":"bad"}`, `{"id":"pred-1","status":"succeeded","output":null}`, `{"id":"pred-1","status":"succeeded","output":{}}`, `{"id":"pred-1","status":"succeeded","output":[null,"text"]}`, `{"id":"pred-1","status":"succeeded","output":" "}`, `{"id":"pred-1","status":"succeeded","error":"private-provider-body","output":"text"}`, `{"id":"pred-1","status":"starting","urls":{"get":"https://untrusted.invalid/poll"}}`} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, response) }))
		m := newReplicateTestModel(srv.URL)
		if _, err := m.ChatWithMessages("owner/model", &APIConfig{APIKey: &key}, []Message{{Role: "user", Content: "hello"}}, nil); err == nil || strings.Contains(err.Error(), "private-provider-body") {
			t.Fatal("malformed prediction accepted or leaked", response, err)
		}
		srv.Close()
	}
	var calls atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
	defer srv.Close()
	m := newReplicateTestModel(srv.URL)
	for _, name := range []string{"", " ", "owner/../model", "../model", "owner/model:bad", "bad-version", "owner/model?query"} {
		if _, err := m.ChatWithMessages(name, &APIConfig{APIKey: &key}, []Message{{Role: "user", Content: "hi"}}, nil); err == nil {
			t.Fatal("invalid name accepted", name)
		}
	}
	if _, err := m.ChatWithMessages("owner/model", nil, []Message{{Role: "user", Content: "hi"}}, nil); err == nil {
		t.Fatal("missing key accepted")
	}
	if calls.Load() != 0 {
		t.Fatal("invalid input called provider")
	}
}

func TestReplicateDiscoveryCancellationAndSender(t *testing.T) {
	key := "key"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/models":
			if r.Method != http.MethodGet || r.ContentLength != 0 {
				t.Error("discovery is not bodyless GET")
			}
			fmt.Fprint(w, `{"results":[{"owner":"owner","name":"model"}]}`)
		case "/v1/models/owner/model/predictions":
			fmt.Fprint(w, `{"id":"pred-1","status":"succeeded","output":"answer"}`)
		default:
			<-r.Context().Done()
		}
	}))
	defer srv.Close()
	m := newReplicateTestModel(srv.URL)
	if names, err := m.ListModels(&APIConfig{APIKey: &key}); err != nil || strings.Join(names, ",") != "owner/model" {
		t.Fatal(names, err)
	}
	if err := m.CheckConnection(&APIConfig{APIKey: &key}); err != nil {
		t.Fatal(err)
	}
	want := errors.New("sender failed")
	if err := m.ChatStreamlyWithMessages("owner/model", []Message{{Role: "user", Content: "hi"}}, &APIConfig{APIKey: &key}, nil, func(*string, *string) error { return want }); !errors.Is(err, want) {
		t.Fatal("sender error lost", err)
	}
	m.URLSuffix.Models = "wait"
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Millisecond)
	defer cancel()
	if _, err := m.ListModels(&APIConfig{APIKey: &key, Context: ctx}); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("deadline lost", err)
	}
}

func TestReplicateTextPartsAndMediaPreflight(t *testing.T) {
	key := "key"
	var calls atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var body struct {
			Input map[string]any `json:"input"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Input["prompt"] != "hello again" {
			t.Error("text parts did not become text", body, err)
		}
		fmt.Fprint(w, `{"id":"pred-1","status":"succeeded","output":"answer"}`)
	}))
	defer srv.Close()
	m := newReplicateTestModel(srv.URL)
	texts := []map[string]any{{"type": "text", "text": "hello "}, {"type": "text", "text": "again"}}
	for _, stream := range []bool{false, true} {
		messages := []Message{{Role: "user", Content: texts}}
		if stream {
			if err := m.ChatStreamlyWithMessages("owner/model", messages, &APIConfig{APIKey: &key}, nil, func(*string, *string) error { return nil }); err != nil {
				t.Fatal(err)
			}
		} else if _, err := m.ChatWithMessages("owner/model", &APIConfig{APIKey: &key}, messages, nil); err != nil {
			t.Fatal(err)
		}
	}
	before := calls.Load()
	for _, kind := range []string{"image_url", "video_url", "file_url", "input_audio"} {
		for _, stream := range []bool{false, true} {
			part := map[string]any{"type": kind, kind: map[string]any{"url": "https://files.invalid/media"}}
			if kind == "input_audio" {
				part[kind] = map[string]any{"data": "eA==", "format": "wav"}
			}
			messages := []Message{{Role: "user", Content: []map[string]any{part}}}
			var err error
			if stream {
				err = m.ChatStreamlyWithMessages("owner/model", messages, &APIConfig{APIKey: &key}, nil, func(*string, *string) error { return nil })
			} else {
				_, err = m.ChatWithMessages("owner/model", &APIConfig{APIKey: &key}, messages, nil)
			}
			if !errors.Is(err, ErrUnsupportedCapability) {
				t.Fatal("media must fail before HTTP", kind, err)
			}
		}
	}
	if calls.Load() != before {
		t.Fatal("unsupported media called provider")
	}
}
