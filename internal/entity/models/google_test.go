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
	"sync/atomic"
	"testing"
	"time"
)

func googleTestConfig() (*string, *string, *APIConfig) {
	name, message, key := "gemini-2.5-flash", "hello", "fixture-key"
	return &name, &message, &APIConfig{APIKey: &key}
}

func TestGoogleChatSDKRequestAndThoughtSeparation(t *testing.T) {
	for _, thinking := range []bool{true, false} {
		t.Run(fmt.Sprint(thinking), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != "POST" || r.URL.Path != "/v1beta/models/gemini-2.5-flash:generateContent" {
					t.Errorf("unexpected SDK endpoint %s %s", r.Method, r.URL.Path)
				}
				if r.Header.Get("x-goog-api-key") != "fixture-key" || r.URL.Query().Has("key") || r.Header.Get("Authorization") != "" {
					t.Error("API key must use SDK header")
				}
				var body struct {
					GenerationConfig struct {
						ThinkingConfig struct {
							IncludeThoughts bool
							ThinkingBudget  *int
						}
					}
					Contents []struct{ Role string }
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if body.GenerationConfig.ThinkingConfig.IncludeThoughts != thinking {
					t.Error("thinking flag not passed to SDK")
				}
				if !thinking && (body.GenerationConfig.ThinkingConfig.ThinkingBudget == nil || *body.GenerationConfig.ThinkingConfig.ThinkingBudget != 0) {
					t.Error("explicit false must disable thinking")
				}
				fmt.Fprint(w, `{"candidates":[{"content":{"parts":[{"text":"thought","thought":true},{"text":"answer"},{"text":" tail"}]}}]}`)
			}))
			defer server.Close()
			g := NewGoogleModel(map[string]string{"default": "http://127.0.0.1:1", "custom": server.URL}, URLSuffix{})
			name, message, api := googleTestConfig()
			region := "custom"
			api.Region = &region
			response, err := g.Chat(name, message, api, &ChatConfig{Thinking: &thinking})
			if err != nil {
				t.Fatal(err)
			}
			if *response.Answer != "answer tail" {
				t.Fatalf("answer %q", *response.Answer)
			}
			want := ""
			if thinking {
				want = "thought"
			}
			if *response.ReasoningContent != want {
				t.Fatalf("reasoning %q", *response.ReasoningContent)
			}
		})
	}
}

func TestGoogleEmptyChatResponses(t *testing.T) {
	for _, response := range []string{`{}`, `{"candidates":[]}`, `{"candidates":[{}]}`, `{"candidates":[{"content":{}}]}`, `{"candidates":[{"content":{"parts":[]}}]}`} {
		t.Run(response, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, response) }))
			defer server.Close()
			g := NewGoogleModel(map[string]string{"default": server.URL}, URLSuffix{})
			name, message, api := googleTestConfig()
			if result, err := g.Chat(name, message, api, nil); err == nil || result != nil {
				t.Fatalf("empty reply returned success: %v %v", result, err)
			}
		})
	}
}

func TestGoogleStreamingEmptyChunksOrderAndCallbackFailure(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1beta/models/gemini-2.5-flash:streamGenerateContent" || r.URL.Query().Get("alt") != "sse" {
			t.Error("wrong SDK stream path")
		}
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		if body["generationConfig"] == nil {
			t.Error("stream dropped generation config")
		}
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {}\n\ndata: {\"candidates\":[{\"content\":{\"parts\":[]}}]}\n\ndata: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"reason\",\"thought\":true},{\"text\":\"answer\"}]}}]}\n\n")
	}))
	defer server.Close()
	g := NewGoogleModel(map[string]string{"default": server.URL}, URLSuffix{})
	name, message, api := googleTestConfig()
	thinking := true
	var events []string
	sender := func(answer, reason *string) error {
		if reason != nil {
			events = append(events, "r:"+*reason)
		}
		if answer != nil {
			events = append(events, "a:"+*answer)
		}
		return nil
	}
	if err := g.ChatStreamlyWithSender(name, message, api, &ChatConfig{Thinking: &thinking}, sender); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(events, []string{"r:reason", "a:answer", "a:[DONE]"}) {
		t.Fatalf("events %v", events)
	}
	failed := errors.New("callback failed")
	if err := g.ChatStreamlyWithSender(name, message, api, nil, func(*string, *string) error { return failed }); !errors.Is(err, failed) {
		t.Fatalf("callback error %v", err)
	}
}

func TestGoogleModelPaginationAndConnection(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.URL.Path != "/v1beta/models" {
			t.Errorf("list path %s", r.URL.Path)
		}
		switch r.URL.Query().Get("pageToken") {
		case "":
			fmt.Fprint(w, `{"models":[{"name":"models/first"}],"nextPageToken":"next"}`)
		case "next":
			fmt.Fprint(w, `{"models":[{"name":"models/second"}]}`)
		default:
			t.Error("wrong page token")
		}
	}))
	defer server.Close()
	g := NewGoogleModel(map[string]string{"default": server.URL}, URLSuffix{})
	_, _, api := googleTestConfig()
	names, err := g.ListModels(api)
	if err != nil || !reflect.DeepEqual(names, []string{"models/first", "models/second"}) {
		t.Fatalf("names %v err %v", names, err)
	}
	if err := g.CheckConnection(api); err != nil {
		t.Fatal(err)
	}
	if requests.Load() != 4 {
		t.Fatalf("requests %d", requests.Load())
	}
}

func TestGooglePageErrorsAndEmptyListing(t *testing.T) {
	for _, mode := range []string{"error", "empty", "cycle"} {
		t.Run(mode, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if mode == "empty" {
					fmt.Fprint(w, `{"models":[]}`)
					return
				}
				if mode == "cycle" || r.URL.Query().Get("pageToken") == "" {
					fmt.Fprint(w, `{"models":[{"name":"first"}],"nextPageToken":"again"}`)
					return
				}
				w.WriteHeader(403)
				fmt.Fprint(w, `{"error":{"code":403,"message":"fixture-key secret internal","status":"PERMISSION_DENIED"}}`)
			}))
			defer server.Close()
			g := NewGoogleModel(map[string]string{"default": server.URL}, URLSuffix{})
			_, _, api := googleTestConfig()
			names, err := g.ListModels(api)
			if mode == "empty" {
				if err != nil || names == nil || len(names) != 0 {
					t.Fatalf("empty %v %v", names, err)
				}
			} else {
				if err == nil || names != nil || strings.Contains(err.Error(), "fixture-key") {
					t.Fatalf("failure %v %v", names, err)
				}
			}
		})
	}
}

func TestGoogleErrorsAreSafeAndCancellationReachesTransport(t *testing.T) {
	started := make(chan struct{}, 1)
	cancelled := make(chan struct{}, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "stream") {
			w.Header().Set("Content-Type", "text/event-stream")
			w.WriteHeader(200)
			w.(http.Flusher).Flush()
			started <- struct{}{}
			<-r.Context().Done()
			cancelled <- struct{}{}
			return
		}
		w.WriteHeader(401)
		fmt.Fprint(w, `{"error":{"code":401,"message":"fixture-key private upstream detail"}}`)
	}))
	defer server.Close()
	g := NewGoogleModel(map[string]string{"default": server.URL}, URLSuffix{})
	name, message, api := googleTestConfig()
	if _, err := g.Chat(name, message, api, nil); err == nil || strings.Contains(err.Error(), "fixture-key") || !strings.Contains(err.Error(), "401") {
		t.Fatalf("unsafe error %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	api.Context = ctx
	done := make(chan error, 1)
	go func() {
		done <- g.ChatStreamlyWithSender(name, message, api, nil, func(*string, *string) error { return nil })
	}()
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("SDK stream not started")
	}
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("cancel error %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("SDK stream did not cancel")
	}
	select {
	case <-cancelled:
	case <-time.After(5 * time.Second):
		t.Fatal("provider request not cancelled")
	}
}

func TestGoogleValidationAndUnsupportedCapabilities(t *testing.T) {
	g := NewGoogleModel(map[string]string{"default": "http://127.0.0.1:1"}, URLSuffix{})
	name, message, api := googleTestConfig()
	blank := "  "
	for _, config := range []*APIConfig{nil, {}, {APIKey: &blank}} {
		if _, err := g.ListModels(config); err == nil {
			t.Fatal("missing key accepted")
		}
	}
	if _, err := g.Chat(nil, message, api, nil); err == nil {
		t.Fatal("nil model accepted")
	}
	if _, err := g.Chat(name, nil, api, nil); err == nil {
		t.Fatal("nil message accepted")
	}
	if err := g.ChatStreamlyWithSender(name, message, api, nil, nil); err == nil {
		t.Fatal("nil sender accepted")
	}
	if values, err := g.Encode(name, []string{"hello"}, api, nil); err == nil || values != nil {
		t.Fatal("unsupported embeddings succeeded")
	}
	if values, err := g.Balance(api); err == nil || values != nil {
		t.Fatal("unsupported balance succeeded")
	}
	if values, err := g.ChatStreamly(name, api.APIKey, message, nil); err == nil || values != nil {
		t.Fatal("unsafe channel succeeded")
	}
	if err := g.ChatStreamlyWithChannel(name, api.APIKey, message, nil, make(chan string)); err == nil {
		t.Fatal("unsafe channel succeeded")
	}
}

func TestGoogleChatWithMessagesPreservesRolesAndSystemInstruction(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Contents          []struct{ Role string }
			SystemInstruction struct{ Parts []struct{ Text string } }
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if len(body.Contents) != 2 || body.Contents[0].Role != "user" || body.Contents[1].Role != "model" || body.SystemInstruction.Parts[0].Text != "rules" {
			t.Errorf("history %#v", body)
		}
		fmt.Fprint(w, `{"candidates":[{"content":{"parts":[{"text":"reply"}]}}]}`)
	}))
	defer server.Close()
	g := NewGoogleModel(map[string]string{"default": server.URL}, URLSuffix{})
	_, _, api := googleTestConfig()
	reply, err := g.ChatWithMessages("gemini-test", api, []Message{{Role: "system", Content: "rules"}, {Role: "user", Content: "hello"}, {Role: "assistant", Content: "old"}}, nil)
	if err != nil || reply == nil || reply.Answer == nil || *reply.Answer != "reply" {
		t.Fatalf("reply %v %v", reply, err)
	}
}

func TestGoogleBaseURLRegionAndFallback(t *testing.T) {
	for _, region := range []string{"regional", "", "missing"} {
		t.Run(region, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, `{"models":[]}`) }))
			defer server.Close()
			urls := map[string]string{"default": server.URL}
			if region == "regional" {
				urls[region] = server.URL
				urls["default"] = "http://127.0.0.1:1"
			}
			if region == "" {
				urls[""] = server.URL
				urls["default"] = "http://127.0.0.1:1"
			}
			g := NewGoogleModel(urls, URLSuffix{})
			_, _, api := googleTestConfig()
			api.Region = &region
			if err := g.CheckConnection(api); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestGoogleHistorySenderPreservesRolesAndContext(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1beta/models/gemini-test:streamGenerateContent" || r.URL.Query().Get("alt") != "sse" {
			t.Errorf("history stream URL %s", r.URL)
		}
		var body struct {
			Contents          []struct{ Role string }
			SystemInstruction struct{ Parts []struct{ Text string } }
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if len(body.Contents) != 3 || body.Contents[0].Role != "user" || body.Contents[1].Role != "model" || body.Contents[2].Role != "user" || body.SystemInstruction.Parts[0].Text != "rules" {
			t.Errorf("history %#v", body)
		}
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"reply\"}]}}]}\n\n")
	}))
	defer server.Close()
	model := NewGoogleModel(map[string]string{"default": "http://127.0.0.1:1", "fixture": server.URL}, URLSuffix{})
	_, _, api := googleTestConfig()
	region := "fixture"
	api.Region = &region
	history := []Message{{Role: "system", Content: "rules"}, {Role: "user", Content: "old"}, {Role: "assistant", Content: "previous"}, {Role: "user", Content: "question"}}
	var frames []string
	err := model.ChatStreamlyWithMessages("gemini-test", history, api, nil, func(content, reason *string) error {
		if content != nil {
			frames = append(frames, *content)
		}
		return nil
	})
	if err != nil || !reflect.DeepEqual(frames, []string{"reply", "[DONE]"}) {
		t.Fatalf("history frames=%v err=%v", frames, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	api.Context = ctx
	if _, err := model.ChatWithMessages("gemini-test", api, history, nil); !errors.Is(err, context.Canceled) {
		t.Fatalf("history cancellation=%v", err)
	}
	if err := model.ChatStreamlyWithMessages("gemini-test", history, api, nil, func(*string, *string) error { return nil }); !errors.Is(err, context.Canceled) {
		t.Fatalf("history stream cancellation=%v", err)
	}
}
