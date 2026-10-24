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
	"time"
)

func TestXinferenceEndpointAuthAndChat(t *testing.T) {
	for _, suffix := range []string{"", "/", "/v1", "/v1/"} {
		for _, key := range []string{"", "  ", "explicit-key"} {
			t.Run(suffix+"/key="+key, func(t *testing.T) {
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					wantAuth := ""
					if strings.TrimSpace(key) != "" {
						wantAuth = "Bearer " + key
					}
					if r.Header.Get("Authorization") != wantAuth {
						t.Error("incorrect optional authentication")
					}
					switch r.URL.Path {
					case "/v1/chat/completions":
						var body map[string]any
						if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
							t.Error(err)
						}
						if body["model"] != "served-chat" || body["stream"] != false || body["temperature"] != float64(0) || body["max_tokens"] != float64(32) || body["enable_thinking"] != nil || body["reasoning_effort"] != nil {
							t.Error("chat fields lost or future capability leaked", body)
						}
						if len(body["messages"].([]any)) != 3 {
							t.Error("history lost")
						}
						fmt.Fprint(w, `{"choices":[{"message":{"content":"answer","reasoning_content":"steps"}}]}`)
					case "/v1/models":
						if r.Method != http.MethodGet || r.ContentLength != 0 {
							t.Error("models request must be bodyless GET")
						}
						fmt.Fprint(w, `{"data":[{"id":"served-chat"}]}`)
					default:
						t.Error("incorrect normalized path", r.URL.Path)
						w.WriteHeader(404)
					}
				}))
				defer srv.Close()
				urls := map[string]string{"default": " " + srv.URL + suffix + " "}
				driver, err := NewModelFactory().CreateModelDriver("Xinference", urls, URLSuffix{Chat: "v1/chat/completions", Models: "v1/models"})
				if err != nil || driver.Name() != "xinference" {
					t.Fatal(driver, err)
				}
				api := &APIConfig{APIKey: &key}
				zero, count, thinking, effort := 0.0, 32, false, "high"
				resp, err := driver.ChatWithMessages("served-chat", api, []Message{{Role: "user", Content: "hello"}, {Role: "assistant", Content: "hi"}, {Role: "user", Content: "again"}}, &ChatConfig{Temperature: &zero, MaxTokens: &count, Thinking: &thinking, Effort: &effort})
				if err != nil || resp.Answer == nil || *resp.Answer != "answer" || resp.ReasoningContent == nil || *resp.ReasoningContent != "steps" {
					t.Fatal(resp, err)
				}
				if names, err := driver.ListModels(api); err != nil || strings.Join(names, ",") != "served-chat" {
					t.Fatal(names, err)
				}
				if err := driver.CheckConnection(api); err != nil || urls["default"] != " "+srv.URL+suffix+" " {
					t.Fatal("connection or catalog isolation failed", err)
				}
			})
		}
	}
}

func TestXinferenceStreamAndReasoning(t *testing.T) {
	for _, field := range []string{"reasoning_content", "reasoning", "thinking"} {
		t.Run(field, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{%q:\"steps\"}}]}\n", field)
				fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"},\"finish_reason\":\"stop\"}]}\n")
			}))
			defer srv.Close()
			x := NewXinferenceModel(map[string]string{"default": srv.URL + "/v1"}, URLSuffix{Chat: "v1/chat/completions"})
			var content, reasoning string
			err := x.ChatStreamlyWithMessages("served-chat", []Message{{Role: "user", Content: "hi"}}, nil, nil, func(c, r *string) error {
				if c != nil {
					content += *c
				}
				if r != nil {
					reasoning += *r
				}
				return nil
			})
			if err != nil || content != "answer[DONE]" || reasoning != "steps" {
				t.Fatal(content, reasoning, err)
			}
		})
	}
}

func TestXinferenceIdleCancellationAndUnsupported(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n")
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}))
	defer srv.Close()
	x := NewXinferenceModel(map[string]string{"default": srv.URL}, URLSuffix{Chat: "v1/chat/completions", Embedding: "future", Rerank: "future"})
	x.streamIdleTimeout = 25 * time.Millisecond
	if err := x.ChatStreamlyWithMessages("served-chat", []Message{{Role: "user", Content: "hi"}}, nil, nil, func(*string, *string) error { return nil }); err == nil {
		t.Fatal("idle stream succeeded")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := x.ChatWithMessages("served-chat", &APIConfig{Context: ctx}, []Message{{Role: "user", Content: "hi"}}, nil); !errors.Is(err, context.Canceled) {
		t.Fatal("request cancellation lost", err)
	}
	if _, err := x.Embed(nil, nil, nil, nil); !errors.Is(err, ErrUnsupportedCapability) {
		t.Fatal("later embedding capability leaked", err)
	}
	if _, err := x.Rerank(nil, "", nil, nil, nil); !errors.Is(err, ErrUnsupportedCapability) {
		t.Fatal("later rerank capability leaked", err)
	}
	stream := false
	if err := x.ChatStreamlyWithMessages("served-chat", []Message{{Role: "user", Content: "hi"}}, nil, &ChatConfig{Stream: &stream}, func(*string, *string) error { return nil }); err == nil {
		t.Fatal("explicit stream=false ignored")
	}
}
