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

func afterf9Driver(t *testing.T, provider, base string) ModelDriver {
	t.Helper()
	d, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": base}, URLSuffix{Chat: "chat", Models: "models", Embedding: "embeddings", Rerank: "rerank", ASR: "asr", TTS: "speech", OCR: "ocr"})
	if err != nil {
		t.Fatal(err)
	}
	return d
}

func TestAfterf9ChatHTTP(t *testing.T) {
	for _, provider := range []string{"HunYuan", "TokenHub", "Groq"} {
		t.Run(provider, func(t *testing.T) {
			srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer key" {
					t.Error("credential lost")
				}
				if r.URL.Path == "/models" {
					if r.Method != "GET" {
						t.Error(r.Method)
					}
					raw, _ := io.ReadAll(r.Body)
					if len(raw) > 0 {
						t.Error("GET body")
					}
					fmt.Fprint(w, `{"data":[{"id":"served"}]}`)
					return
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				if body["model"] != "openai/gpt-oss-20b" || body["temperature"] != float64(0) || body["top_p"] != float64(0) || body["max_tokens"] != float64(0) {
					t.Error(body)
				}
				if provider == "Groq" && (body["include_reasoning"] != true || body["reasoning_effort"] != "low") {
					t.Error("Groq reasoning config lost", body)
				}
				if body["stream"] == true {
					w.Header().Set("Content-Type", "text/event-stream")
					fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"thought\"}}]}\n\ndata: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
				} else {
					fmt.Fprint(w, `{"choices":[{"message":{"content":"answer","reasoning_content":"thought"}}]}`)
				}
			}))
			defer srv.Close()
			d := afterf9Driver(t, provider, srv.URL)
			api := &APIConfig{APIKey: stringPtr("key")}
			zero, tokens, effort := 0.0, 0, "low"
			cfg := &ChatConfig{Temperature: &zero, TopP: &zero, MaxTokens: &tokens, Effort: &effort}
			messages := []Message{{Role: "system", Content: "rules"}, {Role: "user", Content: "question"}}
			resp, err := d.ChatWithMessages("openai/gpt-oss-20b", api, messages, cfg)
			if err != nil || resp == nil || *resp.Answer != "answer" || *resp.ReasoningContent != "thought" {
				t.Fatal(resp, err)
			}
			output, reason := "", ""
			err = d.ChatStreamlyWithMessages("openai/gpt-oss-20b", messages, api, cfg, func(c, r *string) error {
				if c != nil {
					output += *c
				}
				if r != nil {
					reason += *r
				}
				return nil
			})
			if err != nil || output != "answer[DONE]" || reason != "thought" {
				t.Fatal(output, reason, err)
			}
			names, err := d.ListModels(api)
			if err != nil || !reflect.DeepEqual(names, []string{"served"}) {
				t.Fatal(names, err)
			}
		})
	}
	for _, name := range []string{"qwen/qwen3-32b", "deepseek-r1"} {
		body, err := groqChatBody(name, []Message{{Role: "user", Content: "q"}}, nil, false)
		if err != nil || body["reasoning_format"] != "parsed" {
			t.Fatal(body, err)
		}
	}
}

func TestAfterf9ChatFailuresHTTP(t *testing.T) {
	for _, provider := range []string{"HunYuan", "TokenHub", "Groq"} {
		for _, body := range []string{`null`, `{}`, `{"choices":[]}`, `{"choices":[{"message":{"content":" "}}]}`, `{"choices":[{"message":{"content":"ok"}}],"error":"private-secret"}`, `{"choices":[{"message":{"content":"ok"}}],"code":1}`, `{} {}`} {
			t.Run(provider+body, func(t *testing.T) {
				srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
				defer srv.Close()
				result, err := afterf9Driver(t, provider, srv.URL).ChatWithMessages("served", &APIConfig{APIKey: stringPtr("key")}, []Message{{Role: "user", Content: "q"}}, nil)
				if err == nil || result != nil || strings.Contains(err.Error(), "private-secret") {
					t.Fatal(result, err)
				}
			})
		}
		for _, body := range []string{"data: [DONE]\n\n", "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n", "data: garbage\n\ndata: [DONE]\n\n", "data: {\"error\":\"private-secret\"}\n\ndata: [DONE]\n\n"} {
			t.Run(provider+body, func(t *testing.T) {
				srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					w.Header().Set("Content-Type", "text/event-stream")
					fmt.Fprint(w, body)
				}))
				defer srv.Close()
				done := 0
				err := afterf9Driver(t, provider, srv.URL).ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: stringPtr("key")}, nil, func(c, r *string) error {
					if c != nil && *c == "[DONE]" {
						done++
					}
					return nil
				})
				if err == nil || done > 0 || strings.Contains(err.Error(), "private-secret") {
					t.Fatal(done, err)
				}
			})
		}
		for _, body := range []string{`{}`, `null`, `{"data":null}`, `{"data":[{}]}`, `{"data":[{"id":"x"},{"id":"x"}]}`, `{"data":[{"id":" x"}]}`, `{"data":[{"id":"ok"},5]}`, `{"data":[],"error":"private-secret"}`} {
			t.Run(provider+"list"+body, func(t *testing.T) {
				srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
				defer srv.Close()
				names, err := afterf9Driver(t, provider, srv.URL).ListModels(&APIConfig{APIKey: stringPtr("key")})
				if err == nil || names != nil || strings.Contains(err.Error(), "private-secret") {
					t.Fatal(names, err)
				}
			})
		}
	}
}

func TestAfterf9ChatPreflightAndIsolation(t *testing.T) {
	for _, provider := range []string{"HunYuan", "TokenHub", "Groq"} {
		t.Run(provider, func(t *testing.T) {
			var calls atomic.Int32
			srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				fmt.Fprint(w, `{"choices":[{"message":{"content":"ok"}}]}`)
			}))
			defer srv.Close()
			api := &APIConfig{APIKey: stringPtr("key")}
			d := afterf9Driver(t, provider, srv.URL)
			bad := math.NaN()
			negative := -1.0
			over := 1.1
			negativeTokens := -1
			for _, cfg := range []*ChatConfig{{Temperature: &bad}, {TopP: &over}, {Temperature: &negative}, {MaxTokens: &negativeTokens}} {
				if _, err := d.ChatWithMessages("served", api, []Message{{Role: "user", Content: "q"}}, cfg); err == nil {
					t.Fatal("invalid config accepted")
				}
			}
			for _, name := range []string{"", " "} {
				if _, err := d.ChatWithMessages(name, api, []Message{{Role: "user", Content: "q"}}, nil); err == nil {
					t.Fatal("blank name accepted")
				}
			}
			if _, err := d.ChatWithMessages("served", nil, []Message{{Role: "user", Content: "q"}}, nil); err == nil {
				t.Fatal("nil credentials accepted")
			}
			if _, err := d.ChatWithMessages("served", &APIConfig{APIKey: stringPtr("key"), Region: stringPtr("missing")}, []Message{{Role: "user", Content: "q"}}, nil); err == nil {
				t.Fatal("unknown region fell back")
			}
			if calls.Load() != 0 {
				t.Fatal("invalid request reached provider")
			}
			urls := map[string]string{"default": "http://unreachable.invalid", "office": srv.URL}
			d, _ = NewModelFactory().CreateModelDriver(provider, urls, URLSuffix{Chat: "chat"})
			urls["office"] = "http://mutated.invalid"
			if _, err := d.ChatWithMessages("served", &APIConfig{APIKey: stringPtr("key"), Region: stringPtr("office")}, []Message{{Role: "user", Content: "q"}}, nil); err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			_, err := d.ChatWithMessages("served", &APIConfig{APIKey: stringPtr("key"), Region: stringPtr("office"), Context: ctx}, []Message{{Role: "user", Content: "q"}}, nil)
			if !errors.Is(err, context.Canceled) || calls.Load() != 1 {
				t.Fatal(calls.Load(), err)
			}
		})
	}
}

func TestAfterf9ChatSenderAndCancellation(t *testing.T) {
	for _, provider := range []string{"HunYuan", "TokenHub", "Groq"} {
		t.Run(provider, func(t *testing.T) {
			stopped := make(chan struct{})
			srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				io.Copy(io.Discard, r.Body)
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"hello\"}}]}\n\n")
				w.(http.Flusher).Flush()
				select {
				case <-r.Context().Done():
				case <-time.After(2 * time.Second):
					t.Error("provider not canceled")
				}
				close(stopped)
			}))
			defer srv.Close()
			sentinel := errors.New("consumer stopped")
			err := afterf9Driver(t, provider, srv.URL).ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: stringPtr("key")}, nil, func(*string, *string) error { return sentinel })
			if !errors.Is(err, sentinel) {
				t.Fatal(err)
			}
			select {
			case <-stopped:
			case <-time.After(time.Second):
				t.Fatal("provider connection leaked")
			}
		})
	}
}

func TestAfterf9ProviderDefaults(t *testing.T) {
	for _, stream := range []bool{false, true} {
		for _, test := range []struct {
			name  string
			build func(string, []Message, *ChatConfig, bool) (map[string]any, error)
		}{{"HunYuan", validatedHostedChatBody}, {"TokenHub", tokenHubChatBody}} {
			body, err := test.build("served", []Message{{Role: "user", Content: "q"}}, nil, stream)
			if err != nil {
				t.Fatal(err)
			}
			if test.name == "TokenHub" && !stream {
				if body["temperature"] != 0.6 {
					t.Fatal(body)
				}
			} else if _, present := body["temperature"]; present {
				t.Fatal("provider default overwritten", body)
			}
		}
	}
}
