package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestTogetherAIChatReasoningConfiguration(t *testing.T) {
	for _, name := range []string{"openai/gpt-oss-20b", "Qwen/Qwen3-Coder"} {
		t.Run(name, func(t *testing.T) {
			key := "key"
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer key" || r.URL.Path != "/chat/completions" {
					t.Error("wrong authenticated endpoint")
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if body["reasoning"].(map[string]any)["enabled"] != false || body["stream"] != false || body["model"] != name {
					t.Error("explicit false or model lost", body)
				}
				if strings.Contains(name, "gpt-oss") {
					if body["reasoning_effort"] != "high" {
						t.Error("effort lost")
					}
				} else if body["reasoning_effort"] != nil {
					t.Error("effort sent to non-GPT-OSS model")
				}
				fmt.Fprint(w, `{"choices":[{"message":{"content":"answer","reasoning_content":"primary","reasoning":"fallback"}}]}`)
			}))
			defer srv.Close()
			driver, err := NewModelFactory().CreateModelDriver("TogetherAI", map[string]string{"default": srv.URL}, URLSuffix{Chat: "chat/completions"})
			if err != nil || driver.Name() != "togetherai" {
				t.Fatal(driver, err)
			}
			thinking, effort := false, "high"
			resp, err := driver.ChatWithMessages(name, &APIConfig{APIKey: &key}, []Message{{Role: "user", Content: "hi"}}, &ChatConfig{Thinking: &thinking, Effort: &effort})
			if err != nil || *resp.Answer != "answer" || resp.ReasoningContent == nil || *resp.ReasoningContent != "primary" {
				t.Fatal(resp, err)
			}
		})
	}
}

func TestTogetherAIStreamFormatsAndFailures(t *testing.T) {
	for _, tc := range []struct {
		name, events string
		valid        bool
	}{
		{"both-reasoning", "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"primary\",\"reasoning\":\"alias\"}}]}\ndata: {\"choices\":[{\"delta\":{\"content\":\"answer\"},\"finish_reason\":\"stop\"}]}\n", true},
		{"root-finish", "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}],\"finish_reason\":\"stop\"}\n", true},
		{"root-final-frame", "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\ndata: {\"choices\":[],\"finish_reason\":\"stop\"}\n", true},
		{"large-frame", "data: {\"choices\":[{\"delta\":{\"content\":\"" + strings.Repeat("a", 1024*1024+5) + "\"}}]}\ndata: [DONE]\n", true},
		{"malformed", "data: invalid\ndata: [DONE]\n", false},
		{"empty-done", "data: [DONE]\n", false},
		{"provider-error", "data: {\"error\":\"private-provider-body\"}\ndata: [DONE]\n", false},
		{"unexpected-eof", "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			key := "key"
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Accept") != "text/event-stream" {
					t.Error("missing SSE accept")
				}
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprint(w, tc.events)
			}))
			defer srv.Close()
			d := NewTogetherAIModel(map[string]string{"default": srv.URL}, URLSuffix{Chat: "chat"})
			var content, reasoning string
			err := d.ChatStreamlyWithMessages("chat-model", []Message{{Role: "user", Content: "hi"}}, &APIConfig{APIKey: &key}, nil, func(c, r *string) error {
				if c != nil {
					content += *c
				}
				if r != nil {
					reasoning += *r
				}
				return nil
			})
			done := strings.HasSuffix(content, "[DONE]")
			if tc.valid != (err == nil && done) || !tc.valid && (err == nil || done) || err != nil && strings.Contains(err.Error(), "private-provider-body") {
				t.Fatal("wrong stream completion", done, err)
			}
			if tc.name == "both-reasoning" && reasoning != "primaryalias" {
				t.Fatal("one of the reasoning fields lost", reasoning)
			}
		})
	}
}

func TestTogetherAIArrayDiscovery(t *testing.T) {
	for _, response := range []string{`[{"id":"served-chat"}]`, `[]`, `null`, `{}`, `{"data":[{"id":"bad"}]}`, `[{"id":""}]`, `[{"id":"same"},{"id":"same"}]`, `[{"id":null}]`} {
		t.Run(response, func(t *testing.T) {
			key := "key"
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet || r.ContentLength != 0 || r.URL.Path != "/models" {
					t.Error("wrong discovery request")
				}
				fmt.Fprint(w, response)
			}))
			defer srv.Close()
			d := NewTogetherAIModel(map[string]string{"default": srv.URL}, URLSuffix{Models: "models"})
			valid := response == `[{"id":"served-chat"}]` || response == `[]`
			names, err := d.ListModels(&APIConfig{APIKey: &key})
			if valid != (err == nil) || valid && response != `[]` && strings.Join(names, ",") != "served-chat" {
				t.Fatal(names, err)
			}
			if err := d.CheckConnection(&APIConfig{APIKey: &key}); valid != (err == nil) {
				t.Fatal("check must use array discovery", err)
			}
		})
	}
}

func TestTogetherAICancellationAndSender(t *testing.T) {
	key := "key"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n")
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}))
	defer srv.Close()
	d := NewTogetherAIModel(map[string]string{"default": srv.URL}, URLSuffix{Chat: "chat"})
	want := errors.New("sender failed")
	if err := d.ChatStreamlyWithMessages("model", []Message{{Role: "user", Content: "hi"}}, &APIConfig{APIKey: &key}, nil, func(*string, *string) error { return want }); !errors.Is(err, want) {
		t.Fatal("sender error lost", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	if err := d.ChatStreamlyWithMessages("model", []Message{{Role: "user", Content: "hi"}}, &APIConfig{APIKey: &key, Context: ctx}, nil, func(*string, *string) error { cancel(); return nil }); !errors.Is(err, context.Canceled) {
		t.Fatal("stream cancellation lost", err)
	}
	stream := false
	if err := d.ChatStreamlyWithMessages("model", []Message{{Role: "user", Content: "hi"}}, &APIConfig{APIKey: &key}, &ChatConfig{Stream: &stream}, func(*string, *string) error { return nil }); err == nil {
		t.Fatal("stream=false accepted")
	}
}
