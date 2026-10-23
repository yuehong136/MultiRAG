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

func TestAfter9dHostedChatHTTP(t *testing.T) {
	for _, provider := range []string{"CometAPI"} {
		t.Run(provider, func(t *testing.T) {
			fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/office/chat" || r.Header.Get("Authorization") != "Bearer key" || r.Header.Get("Content-Type") != "application/json" {
					t.Error("instance lost", r.URL.Path)
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				messages, _ := body["messages"].([]any)
				if len(messages) != 3 || messages[0].(map[string]any)["role"] != "system" || messages[1].(map[string]any)["role"] != "assistant" || body["model"] != "model" || body["max_tokens"] != float64(64) || body["temperature"] != 0.3 || body["top_p"] != 0.9 || !reflect.DeepEqual(body["stop"], []any{"END"}) {
					t.Error("history/config lost", body)
				}
				if body["stream"] == true {
					w.Header().Set("Content-Type", "text/event-stream")
					fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"reason\"}}]}\n\ndata: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\ndata: {\"choices\":[],\"usage\":{\"total_tokens\":0}}\n\ndata: [DONE]\n\n")
				} else {
					fmt.Fprint(w, `{"choices":[{"message":{"content":"answer","reasoning_content":"reason"}}]}`)
				}
			}))
			defer fixture.Close()
			urls := map[string]string{"default": "http://unused.invalid", "office": fixture.URL + "/office/"}
			d, err := NewModelFactory().CreateModelDriver(provider, urls, URLSuffix{Chat: "chat"})
			if err != nil {
				t.Fatal(err)
			}
			urls["office"] = "http://mutated.invalid"
			api := &APIConfig{APIKey: stringPtr("key"), Region: stringPtr("office")}
			limit, temp, top, stop := 64, 0.3, 0.9, []string{"END"}
			config := &ChatConfig{MaxTokens: &limit, Temperature: &temp, TopP: &top, Stop: &stop}
			history := []Message{{Role: "system", Content: "rules"}, {Role: "assistant", Content: "old"}, {Role: "user", Content: "q"}}
			answer, err := d.ChatWithMessages("model", api, history, config)
			if err != nil || answer == nil || *answer.Answer != "answer" || *answer.ReasoningContent != "reason" {
				t.Fatal(answer, err)
			}
			var output, reason string
			done := 0
			err = d.ChatStreamlyWithMessages("model", history, api, config, func(c, r *string) error {
				if c != nil {
					if *c == "[DONE]" {
						done++
					} else {
						output += *c
					}
				}
				if r != nil {
					reason += *r
				}
				return nil
			})
			if err != nil || output != "answer" || reason != "reason" || done != 1 {
				t.Fatal(output, reason, done, err)
			}
		})
	}
}

func TestAfter9dHostedChatFailuresHTTP(t *testing.T) {
	for _, provider := range []string{"CometAPI"} {
		t.Run(provider, func(t *testing.T) {
			for _, body := range []string{`null`, `{}`, `{"choices":[]}`, `{"choices":[{"message":{"content":" "}}]}`, `{"choices":[{"message":{"content":5}}]}`, `{"choices":[{"message":{"content":"ok"}}],"error":{"message":"private-body"}}`, `{"choices":[{"message":{"content":"ok"}}]} {}`} {
				t.Run(body, func(t *testing.T) {
					fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
					defer fixture.Close()
					d, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": fixture.URL}, URLSuffix{Chat: "chat"})
					answer, err := d.ChatWithMessages("model", &APIConfig{APIKey: stringPtr("key")}, []Message{{Role: "user", Content: "q"}}, nil)
					if err == nil || answer != nil || strings.Contains(err.Error(), "private-body") {
						t.Fatal(answer, err)
					}
				})
			}
			for _, body := range []string{
				"data: [DONE]\n\n",
				"data: {\"choices\":[{\"delta\":{\"content\":\"half\"}}]}\n\n",
				"data: {\"choices\":[{\"delta\":{\"content\":\"half\"}}]}\n\ndata: bad\n\ndata: [DONE]\n\n",
				"data: {\"choices\":[{\"delta\":{\"content\":\"half\"}}]}\n\ndata: {\"error\":{\"message\":\"private-body\"}}\n\ndata: [DONE]\n\n",
				"data: {\"choices\":[{\"delta\":{\"content\":\"answer\"},\"finish_reason\":\"stop\"}]}\n\ndata: {\"error\":\"private-body\"}\n\n",
			} {
				t.Run(body, func(t *testing.T) {
					fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						w.Header().Set("Content-Type", "text/event-stream")
						fmt.Fprint(w, body)
					}))
					defer fixture.Close()
					d, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": fixture.URL}, URLSuffix{Chat: "chat"})
					done := 0
					err := d.ChatStreamlyWithMessages("model", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: stringPtr("key")}, nil, func(c, r *string) error {
						if c != nil && *c == "[DONE]" {
							done++
						}
						return nil
					})
					if err == nil || done != 0 || strings.Contains(err.Error(), "private-body") {
						t.Fatal(err, done)
					}
				})
			}
			fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
			}))
			defer fixture.Close()
			d, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": fixture.URL}, URLSuffix{Chat: "chat"})
			marker := errors.New("sender failed")
			if err := d.ChatStreamlyWithSender(stringPtr("model"), stringPtr("q"), &APIConfig{APIKey: stringPtr("key")}, nil, func(*string, *string) error { return marker }); !errors.Is(err, marker) {
				t.Fatal(err)
			}
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			done := 0
			err := d.ChatStreamlyWithMessages("model", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil, func(c, r *string) error {
				if c != nil && *c == "[DONE]" {
					done++
				}
				cancel()
				return nil
			})
			if !errors.Is(err, context.Canceled) || done != 0 {
				t.Fatal(err, done)
			}
		})
	}
}
