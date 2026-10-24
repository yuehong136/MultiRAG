package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type after2836ChatProvider struct{ name, baseSuffix, chat, models string }

func after2836ChatProviders() []after2836ChatProvider {
	return []after2836ChatProvider{
		{"n1n", "", "v1/chat/completions", "v1/models"},
		{"GPUStack", "/v1/", "v1/chat/completions", "v1/models"},
		{"Azure-OpenAI", "/openai", "chat/completions", "models"},
	}
}

func (p after2836ChatProvider) driver(base string) ModelDriver {
	driver, _ := NewModelFactory().CreateModelDriver(p.name, map[string]string{"default": base + p.baseSuffix}, URLSuffix{Chat: p.chat, Models: p.models, Embedding: "embeddings"})
	return driver
}

func (p after2836ChatProvider) assertRequest(t *testing.T, r *http.Request, name string, discovery bool) {
	t.Helper()
	path := "/" + p.chat
	if discovery {
		path = "/" + p.models
	}
	if p.name == "Azure-OpenAI" {
		path = "/openai/models"
		if !discovery {
			path = "/openai/deployments/" + url.PathEscape(name) + "/" + p.chat
		}
		if r.URL.Query().Get("api-version") != azureOpenAIAPIVersion || len(r.URL.Query()) != 1 || r.Header.Get("api-key") != "key" || r.Header.Get("Authorization") != "" {
			t.Error("Azure version or auth lost", r.URL)
		}
	} else if r.Header.Get("Authorization") != "Bearer key" || r.Header.Get("api-key") != "" || len(r.URL.Query()) != 0 {
		t.Error("Bearer auth lost", r.URL)
	}
	if r.URL.EscapedPath() != path {
		t.Error("provider path or deployment escaping lost", r.URL.EscapedPath(), path)
	}
}

func TestAfter2836NewChatPayloads(t *testing.T) {
	for _, p := range after2836ChatProviders() {
		for _, thinking := range []bool{false, true} {
			t.Run(fmt.Sprint(p.name, thinking), func(t *testing.T) {
				name := "tenant-deployment"
				if p.name == "Azure-OpenAI" {
					name = "deployment/a?#"
				}
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					p.assertRequest(t, r, name, false)
					var body map[string]any
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
					}
					wantName := any(name)
					if p.name == "Azure-OpenAI" {
						wantName = nil
					}
					if r.Method != http.MethodPost || body["model"] != wantName || body["stream"] != false || body["temperature"] != float64(0) || body["max_tokens"] != float64(32) || body["top_p"] != float64(0.5) || len(body["messages"].([]any)) != 2 || len(body["stop"].([]any)) != 1 {
						t.Error("chat request fields lost", body)
					}
					parts := body["messages"].([]any)[1].(map[string]any)["content"].([]any)
					if len(parts) != 2 || parts[0].(map[string]any)["text"] != "describe" || parts[1].(map[string]any)["image_url"].(map[string]any)["url"] != "https://files.invalid/image.png" {
						t.Error("multimodal content changed", parts)
					}
					if p.name == "n1n" {
						mode := "disabled"
						if thinking {
							mode = "enabled"
						}
						if body["thinking"].(map[string]any)["type"] != mode {
							t.Error("n1n thinking lost")
						}
					} else if body["thinking"] != nil {
						t.Error("unsupported thinking object leaked")
					}
					fmt.Fprint(w, `{"choices":[{"message":{"content":"answer🙂","reasoning_content":"reason"}}]}`)
				}))
				defer srv.Close()
				driver := p.driver(srv.URL)
				zero, top, tokens, stop := 0.0, 0.5, 32, []string{"end"}
				messages := []Message{{Role: "system", Content: "system"}, {Role: "user", Content: []map[string]any{{"type": "text", "text": "describe"}, {"type": "image_url", "image_url": map[string]any{"url": "https://files.invalid/image.png"}}}}}
				response, err := driver.ChatWithMessages(name, &APIConfig{APIKey: stringPtr("key")}, messages, &ChatConfig{Thinking: &thinking, Temperature: &zero, TopP: &top, MaxTokens: &tokens, Stop: &stop})
				if err != nil || response.Answer == nil || *response.Answer != "answer🙂" || response.ReasoningContent == nil || *response.ReasoningContent != "reason" {
					t.Fatal(response, err)
				}
			})
		}
	}
}

func TestAfter2836NewChatStrictDiscovery(t *testing.T) {
	for _, p := range after2836ChatProviders() {
		for _, raw := range []string{`{"data":[{"id":"served"}]}`, `{"data":[]}`, `{}`, `null`, `{"data":null}`, `{"data":[null]}`, `{"data":[{}]}`, `{"data":[{"id":null}]}`, `{"data":[{"id":" served"}]}`, `{"data":[{"id":"same"},{"id":"same"}]}`, `{"data":[],"error":"private-secret"}`, `{"data":[]} {}`} {
			t.Run(p.name+"/"+raw, func(t *testing.T) {
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					p.assertRequest(t, r, "", true)
					if r.Method != http.MethodGet || r.ContentLength != 0 {
						t.Error("discovery must be bodyless GET")
					}
					fmt.Fprint(w, raw)
				}))
				defer srv.Close()
				driver := p.driver(srv.URL)
				api := &APIConfig{APIKey: stringPtr("key")}
				valid := raw == `{"data":[{"id":"served"}]}` || raw == `{"data":[]}`
				result, err := driver.ListModels(api)
				if valid != (err == nil) || valid && result == nil || err != nil && strings.Contains(err.Error(), "private-secret") {
					t.Fatal(result, err)
				}
				if err := driver.CheckConnection(api); valid != (err == nil) {
					t.Fatal("connection bypassed strict discovery", err)
				}
			})
		}
	}
}

func TestAfter2836NewChatStreams(t *testing.T) {
	for _, p := range after2836ChatProviders() {
		for _, test := range []struct {
			name, events string
			valid        bool
		}{
			{"done", "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"reason\"}}]}\ndata: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\ndata: [DONE]\n", true},
			{"finish", "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"},\"finish_reason\":\"stop\"}]}\n", true},
			{"bad-json", "data: invalid\ndata: [DONE]\n", false},
			{"error", "data: {\"error\":\"private-secret\"}\n", false},
			{"truncated", "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n", false},
			{"empty", "data: [DONE]\n", false},
			{"no-choices", "data: {}\ndata: [DONE]\n", false},
			{"null-choice", "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\ndata: {\"choices\":[null]}\ndata: [DONE]\n", false},
			{"missing-delta", "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\ndata: {\"choices\":[{}]}\ndata: [DONE]\n", false},
			{"null-delta", "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\ndata: {\"choices\":[{\"delta\":null}]}\ndata: [DONE]\n", false},
		} {
			t.Run(p.name+"/"+test.name, func(t *testing.T) {
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					p.assertRequest(t, r, "served", false)
					var body map[string]any
					json.NewDecoder(r.Body).Decode(&body)
					if body["stream"] != true || r.Header.Get("Accept") != "text/event-stream" {
						t.Error("stream request lost")
					}
					w.Header().Set("Content-Type", "text/event-stream")
					fmt.Fprint(w, test.events)
				}))
				defer srv.Close()
				var answer, reasoning string
				err := p.driver(srv.URL).ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "hi"}}, &APIConfig{APIKey: stringPtr("key")}, nil, func(c, r *string) error {
					if c != nil {
						answer += *c
					}
					if r != nil {
						reasoning += *r
					}
					return nil
				})
				if test.valid != (err == nil && strings.HasSuffix(answer, "[DONE]")) || !test.valid && strings.Contains(answer, "[DONE]") || err != nil && strings.Contains(err.Error(), "private-secret") || test.name == "done" && reasoning != "reason" {
					t.Fatal(answer, reasoning, err)
				}
			})
		}
	}
}

func TestAfter2836NewChatCancellationIdleAndSender(t *testing.T) {
	for _, p := range after2836ChatProviders() {
		for _, mode := range []string{"cancel", "idle", "sender"} {
			t.Run(p.name+"/"+mode, func(t *testing.T) {
				closed := make(chan struct{})
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					defer close(closed)
					fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n")
					w.(http.Flusher).Flush()
					<-r.Context().Done()
				}))
				defer srv.Close()
				driver := p.driver(srv.URL)
				if mode == "idle" {
					switch d := driver.(type) {
					case *N1NModel:
						d.streamIdleTimeout = 25 * time.Millisecond
					case *GPUStackModel:
						d.streamIdleTimeout = 25 * time.Millisecond
					case *AzureOpenAIModel:
						d.streamIdleTimeout = 25 * time.Millisecond
					}
				}
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				marker := errors.New("sender stopped")
				done := false
				err := driver.ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "hi"}}, &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil, func(c, r *string) error {
					if c != nil && *c == "[DONE]" {
						done = true
					}
					if mode == "cancel" {
						cancel()
					}
					if mode == "sender" {
						return marker
					}
					return nil
				})
				want := error(context.Canceled)
				if mode == "idle" {
					want = context.DeadlineExceeded
				}
				if mode == "sender" {
					want = marker
				}
				if !errors.Is(err, want) || done {
					t.Fatal("stream false success or error lost", err, done)
				}
				select {
				case <-closed:
				case <-time.After(time.Second):
					t.Fatal("upstream request leaked after stream stopped")
				}
			})
		}
	}
}

func TestAfter2836NewChatCredentialsFailuresAndRedirect(t *testing.T) {
	for _, p := range after2836ChatProviders() {
		t.Run(p.name, func(t *testing.T) {
			var leaked atomic.Int32
			target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { leaked.Add(1) }))
			defer target.Close()
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				http.Redirect(w, r, target.URL, http.StatusTemporaryRedirect)
			}))
			defer srv.Close()
			driver := p.driver(srv.URL)
			if _, err := driver.ChatWithMessages("served", nil, []Message{{Role: "user", Content: "hi"}}, nil); err == nil {
				t.Fatal("missing key accepted")
			}
			if _, err := driver.ListModels(nil); err == nil {
				t.Fatal("missing discovery key accepted")
			}
			api := &APIConfig{APIKey: stringPtr("key")}
			if _, err := driver.ChatWithMessages("served", api, []Message{{Role: "user", Content: "hi"}}, nil); err == nil || leaked.Load() != 0 {
				t.Fatal("redirect followed with credential", err)
			}
			stream := false
			if err := driver.ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "hi"}}, api, &ChatConfig{Stream: &stream}, func(*string, *string) error { return nil }); err == nil {
				t.Fatal("explicit stream=false ignored")
			}
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			if _, err := driver.ChatWithMessages("served", &APIConfig{Context: ctx, APIKey: stringPtr("key")}, []Message{{Role: "user", Content: "hi"}}, nil); !errors.Is(err, context.Canceled) {
				t.Fatal("cancel lost", err)
			}
			if p.name == "Azure-OpenAI" {
				for _, name := range []string{"", " ", ".", "..", " padded"} {
					if _, err := driver.ChatWithMessages(name, api, []Message{{Role: "user", Content: "hi"}}, nil); err == nil {
						t.Fatal("invalid deployment accepted", name)
					}
				}
			}
		})
	}
}

func TestAfter2836NewChatParentDeadlineAndActiveStream(t *testing.T) {
	for _, p := range after2836ChatProviders() {
		t.Run(p.name+"/parent-deadline", func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n")
				w.(http.Flusher).Flush()
				<-r.Context().Done()
			}))
			defer srv.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
			defer cancel()
			err := p.driver(srv.URL).ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "hi"}}, &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil, func(*string, *string) error { return nil })
			if !errors.Is(err, context.DeadlineExceeded) || strings.Contains(err.Error(), "idle timeout") {
				t.Fatal("parent deadline mislabeled as idle", err)
			}
		})
		t.Run(p.name+"/active-stream", func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				for range 8 {
					fmt.Fprint(w, ": heartbeat\n")
					w.(http.Flusher).Flush()
					time.Sleep(10 * time.Millisecond)
				}
				fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"},\"finish_reason\":\"stop\"}]}\n")
			}))
			defer srv.Close()
			driver := p.driver(srv.URL)
			switch d := driver.(type) {
			case *N1NModel:
				d.streamIdleTimeout = 50 * time.Millisecond
			case *GPUStackModel:
				d.streamIdleTimeout = 50 * time.Millisecond
			case *AzureOpenAIModel:
				d.streamIdleTimeout = 50 * time.Millisecond
			}
			if err := driver.ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "hi"}}, &APIConfig{APIKey: stringPtr("key")}, nil, func(*string, *string) error { return nil }); err != nil {
				t.Fatal("working stream hit total timeout", err)
			}
		})
	}
}

func TestAfter2836StrictChatFinishReasons(t *testing.T) {
	for _, p := range after2836ChatProviders() {
		for _, reason := range []string{"stop", "length", "content_filter", "error", "unknown", "tool_calls"} {
			t.Run(p.name+"/"+reason, func(t *testing.T) {
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					var body map[string]any
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
					}
					if body["stream"] == true {
						fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"first\"}}]}\n")
						fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":\"last\"},\"finish_reason\":%q}]}\n", reason)
					} else {
						fmt.Fprintf(w, `{"choices":[{"message":{"content":"answer"},"finish_reason":%q}]}`, reason)
					}
				}))
				defer srv.Close()
				driver := p.driver(srv.URL)
				valid := reason == "stop" || reason == "length"
				result, err := driver.ChatWithMessages("served", &APIConfig{APIKey: stringPtr("key")}, []Message{{Role: "user", Content: "hi"}}, nil)
				if valid != (err == nil) || !valid && result != nil {
					t.Fatal("nonstream invalid terminal succeeded", result, err)
				}
				answer := ""
				err = driver.ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "hi"}}, &APIConfig{APIKey: stringPtr("key")}, nil, func(c, r *string) error {
					if c != nil {
						answer += *c
					}
					return nil
				})
				if valid != (err == nil && answer == "firstlast[DONE]") || !valid && (strings.Contains(answer, "[DONE]") || strings.Contains(answer, "last")) {
					t.Fatal("stream invalid terminal succeeded", answer, err)
				}
			})
		}
	}
}
