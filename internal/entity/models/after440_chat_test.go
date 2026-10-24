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
	"sync"
	"testing"
	"time"
)

func after440ChatDriver(name, base string) (ModelDriver, *openAIChatDriver) {
	suffix := URLSuffix{Chat: "v1/chat/completions", Models: "v1/models"}
	if name == "Perplexity" {
		suffix.Chat = "v1/sonar"
	}
	if name == "Astraflow" {
		base += "/v1"
		suffix.Chat = "chat/completions"
		suffix.Models = "models"
	}
	driver, _ := NewModelFactory().CreateModelDriver(name, map[string]string{"default": base}, suffix)
	switch d := driver.(type) {
	case *AvianModel:
		return d, d.openAIChatDriver
	case *AstraflowModel:
		return d, d.openAIChatDriver
	case *PerplexityModel:
		return d, d.openAIChatDriver
	}
	panic("unknown provider")
}

func TestAfter440ChatPayloadReasoningAndIsolation(t *testing.T) {
	for _, provider := range []string{"Avian", "Astraflow", "Perplexity"} {
		t.Run(provider, func(t *testing.T) {
			var wg sync.WaitGroup
			for _, key := range []string{"one", "two"} {
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.Header.Get("Authorization") != "Bearer "+key {
						t.Error("instance key crossed")
					}
					var body map[string]any
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
						return
					}
					path := "/v1/chat/completions"
					if provider == "Perplexity" {
						path = "/v1/sonar"
					}
					if r.URL.Path != path || body["model"] != "served" || body["temperature"] != float64(0) || body["top_p"] != float64(0) || len(body["messages"].([]any)) != 4 {
						t.Error("payload lost", body, r.URL)
					}
					if body["stream"] == true {
						fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"reason\",\"reasoning\":\"fallback\",\"content\":%q}}]}\n\ndata: [DONE]\n\n", key)
					} else {
						fmt.Fprintf(w, `{"choices":[{"message":{"content":%q,"reasoning":"fallback"}}]}`, key)
					}
				}))
				defer srv.Close()
				driver, _ := after440ChatDriver(provider, srv.URL)
				wg.Go(func() {
					api := &APIConfig{APIKey: &key}
					zero := 0.0
					messages := []Message{{Role: "system", Content: "s"}, {Role: "user", Content: "q"}, {Role: "assistant", Content: "a"}, {Role: "user", Content: "q2"}}
					response, err := driver.ChatWithMessages("served", api, messages, &ChatConfig{Temperature: &zero, TopP: &zero})
					if err != nil || response.Answer == nil || *response.Answer != key || response.ReasoningContent == nil || *response.ReasoningContent != "fallback" {
						t.Error(response, err)
						return
					}
					var frames []string
					err = driver.ChatStreamlyWithMessages("served", messages, api, &ChatConfig{Temperature: &zero, TopP: &zero}, moonshotFrameCollector(&frames))
					if err != nil || !reflect.DeepEqual(frames, []string{"reason:reason", key, "[DONE]"}) {
						t.Error(frames, err)
					}
				})
			}
			wg.Wait()
		})
	}
}

func TestAfter440StrictDiscovery(t *testing.T) {
	for _, provider := range []string{"Avian", "Astraflow", "Perplexity"} {
		for _, body := range []string{`[]`, `[{"id":"m"}]`, `null`, `[null]`, `[{}]`, `[{"id":""}]`, `[{"id":" m"}]`, `[{"id":"m"},{"id":"m"}]`, `[5]`, `{"error":{"message":"private-secret"}}`, `[] {}`} {
			t.Run(provider+"/"+body, func(t *testing.T) {
				raw := body
				if provider != "Avian" {
					raw = `{"data":` + body + `}`
				}
				valid := body == `[]` || body == `[{"id":"m"}]`
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.Method != "GET" || r.ContentLength != 0 || r.Header.Get("Authorization") != "Bearer key" || r.URL.Path != "/v1/models" {
						t.Error("bad discovery request")
					}
					fmt.Fprint(w, raw)
				}))
				defer srv.Close()
				d, _ := after440ChatDriver(provider, srv.URL)
				names, err := d.ListModels(&APIConfig{APIKey: stringPtr("key")})
				if valid != (err == nil) || valid && names == nil || err != nil && strings.Contains(err.Error(), "private-secret") {
					t.Fatal(names, err)
				}
				if err := d.CheckConnection(&APIConfig{APIKey: stringPtr("key")}); (err == nil) != valid {
					t.Fatal(err)
				}
			})
		}
		for _, body := range []string{`{}`, `{"data":null}`, `{"data":[],"error":"private-secret"}`} {
			t.Run(provider+"/missing/"+body, func(t *testing.T) {
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
				defer srv.Close()
				d, _ := after440ChatDriver(provider, srv.URL)
				if _, err := d.ListModels(&APIConfig{APIKey: stringPtr("key")}); err == nil {
					t.Fatal("invalid list accepted")
				}
			})
		}
	}
}

func TestAfter440ChatFailures(t *testing.T) {
	for _, provider := range []string{"Avian", "Astraflow", "Perplexity"} {
		for _, body := range []string{`{}`, `{"choices":[{"message":{"content":""}}]}`, `{"choices":[{"message":{"content":42}}]}`, `{"error":"private-secret"}`, `{"choices":[{"message":{"content":"a"}}]} {}`} {
			t.Run(provider+body, func(t *testing.T) {
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
				defer srv.Close()
				d, _ := after440ChatDriver(provider, srv.URL)
				response, err := d.ChatWithMessages("served", &APIConfig{APIKey: stringPtr("key")}, []Message{{Role: "user", Content: "q"}}, nil)
				if err == nil || response != nil || strings.Contains(err.Error(), "private-secret") {
					t.Fatal(response, err)
				}
			})
		}
		for _, body := range []string{"data: [DONE]\n\n", "data: nope\n\n", "data: {\"choices\":[null]}\n\n", "data: {\"choices\":[{\"delta\":{\"content\":\"a\"}}]}\n\n", "data: {\"error\":\"private-secret\"}\n\n"} {
			t.Run(provider+body, func(t *testing.T) {
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
				defer srv.Close()
				d, _ := after440ChatDriver(provider, srv.URL)
				done := false
				err := d.ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: stringPtr("key")}, nil, func(c, r *string) error {
					if c != nil && *c == "[DONE]" {
						done = true
					}
					return nil
				})
				if err == nil || done || strings.Contains(err.Error(), "private-secret") {
					t.Fatal(err, done)
				}
			})
		}
	}
}

func TestAfter440StreamLifecycle(t *testing.T) {
	for _, provider := range []string{"Avian", "Astraflow", "Perplexity"} {
		for _, mode := range []string{"idle", "cancel", "sender", "active", "deadline"} {
			t.Run(provider+mode, func(t *testing.T) {
				closed := make(chan struct{})
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					defer close(closed)
					if mode == "active" {
						for range 10 {
							fmt.Fprint(w, ":heartbeat\n")
							w.(http.Flusher).Flush()
							time.Sleep(10 * time.Millisecond)
						}
					}
					fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"a\"}}]}\n\n")
					w.(http.Flusher).Flush()
					if mode == "active" {
						fmt.Fprint(w, "data: [DONE]\n\n")
						return
					}
					<-r.Context().Done()
				}))
				defer srv.Close()
				d, shared := after440ChatDriver(provider, srv.URL)
				shared.streamIdleTimeout = 50 * time.Millisecond
				ctx, cancel := context.WithCancel(t.Context())
				defer cancel()
				if mode == "deadline" {
					var stop context.CancelFunc
					ctx, stop = context.WithTimeout(ctx, 25*time.Millisecond)
					defer stop()
				}
				marker := errors.New("sender stopped")
				done := false
				err := d.ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil, func(c, r *string) error {
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
				switch mode {
				case "active":
					if err != nil || !done {
						t.Fatal(err, done)
					}
				case "idle", "deadline":
					if !errors.Is(err, context.DeadlineExceeded) || done {
						t.Fatal(err, done)
					}
				case "cancel":
					if !errors.Is(err, context.Canceled) || done {
						t.Fatal(err, done)
					}
				case "sender":
					if err != marker || done {
						t.Fatal(err, done)
					}
				}
				select {
				case <-closed:
				case <-time.After(time.Second):
					t.Fatal("request leaked")
				}
			})
		}
	}
}

func TestAfter440TerminalEventsWithoutDelta(t *testing.T) {
	for _, provider := range []string{"Avian", "Astraflow", "Perplexity"} {
		for _, root := range []bool{false, true} {
			if root && provider == "Astraflow" {
				continue
			}
			for _, reason := range []string{"stop", "error"} {
				t.Run(fmt.Sprint(provider, root, reason), func(t *testing.T) {
					srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"a\"}}]}\n\n")
						if root {
							fmt.Fprintf(w, "data: {\"choices\":[],\"finish_reason\":%q}\n\n", reason)
						} else {
							fmt.Fprintf(w, "data: {\"choices\":[{\"finish_reason\":%q}]}\n\n", reason)
						}
					}))
					defer srv.Close()
					d, _ := after440ChatDriver(provider, srv.URL)
					done := false
					err := d.ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: stringPtr("key")}, nil, func(c, r *string) error {
						if c != nil && *c == "[DONE]" {
							done = true
						}
						return nil
					})
					if reason == "stop" {
						if err != nil || !done {
							t.Fatal(err, done)
						}
					} else if err == nil || done {
						t.Fatal("failed terminal accepted", err, done)
					}
				})
			}
		}
	}
}
