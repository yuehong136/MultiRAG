package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestBaiduChatHTTP(t *testing.T) {
	testHostedChatHTTP(t, "Baidu", func(t *testing.T, body map[string]any) {
		if !reflect.DeepEqual(body["thinking"], map[string]any{"type": "disabled"}) || body["enable_thinking"] != nil {
			t.Fatal("Baidu thinking configuration lost", body)
		}
		if body["stream"] == true && body["do_sample"] != false || body["stream"] != true && body["do_sample"] != nil {
			t.Fatal("Baidu stream-specific do_sample lost", body)
		}
	})
}

func TestBaiduChatFailuresHTTP(t *testing.T) { testHostedChatFailures(t, "Baidu", true) }
func TestBaiduCancellationHTTP(t *testing.T) { testHostedCancellation(t, "Baidu", true) }

func TestBaiduThinkingHTTP(t *testing.T) {
	on, off := true, false
	for _, test := range []struct {
		name, effort string
		thinking     *bool
		want         any
		enable       any
		wantEffort   any
	}{
		{"qwen3-32b", "max", &on, nil, true, nil},
		{"ERNIE-5.0", "", &off, nil, false, nil},
		{"deepseek-v3.2", "", &on, map[string]any{"type": "enabled"}, nil, nil},
		{"deepseek-v3.2", "high", &off, map[string]any{"type": "disabled"}, nil, nil},
		{"deepseek-v4-pro", "", &on, map[string]any{"type": "enabled"}, nil, "high"},
		{"deepseek-v4-pro", "default", &on, map[string]any{"type": "enabled"}, nil, "high"},
		{"deepseek-v4-flash", "high", &on, map[string]any{"type": "enabled"}, nil, "high"},
		{"deepseek-v4-pro", "max", &on, map[string]any{"type": "enabled"}, nil, "max"},
		{"deepseek-v4-pro", "low", &on, map[string]any{"type": "disabled"}, nil, nil},
		{"deepseek-v4-pro", "medium", &on, map[string]any{"type": "disabled"}, nil, nil},
		{"deepseek-v4-pro", "none", &on, map[string]any{"type": "disabled"}, nil, nil},
		{"deepseek-v4-pro", "custom", &on, map[string]any{"type": "enabled"}, nil, "custom"},
		{"deepseek-v4-pro", "max", &off, map[string]any{"type": "disabled"}, nil, nil},
		{"deepseek-v4-pro", "max", nil, nil, nil, nil},
	} {
		t.Run(test.name+"/"+test.effort+fmt.Sprint(test.want), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if !reflect.DeepEqual(body["thinking"], test.want) || body["enable_thinking"] != test.enable || body["reasoning_effort"] != test.wantEffort || body["temperature"] != float64(1) {
					t.Errorf("unexpected Baidu options: %v", body)
				}
				if body["stream"] == true {
					fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"reason\",\"content\":\"answer\"},\"finish_reason\":\"stop\"}]}\n\n")
				} else {
					fmt.Fprint(w, `{"choices":[{"message":{"content":"answer","reasoning_content":"\nreason"}}]}`)
				}
			}))
			defer server.Close()
			driver := NewBaiduModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat/completions"})
			api := &APIConfig{APIKey: stringPtr("fixture")}
			config := &ChatConfig{Thinking: test.thinking}
			if test.effort != "" {
				config.Effort = &test.effort
			}
			messages := []Message{{Role: "user", Content: "q"}}
			response, err := driver.ChatWithMessages(test.name, api, messages, config)
			if err != nil || response == nil || response.ReasoningContent == nil || *response.ReasoningContent != "reason" {
				t.Fatal(response, err)
			}
			if err := driver.ChatStreamlyWithMessages(test.name, messages, api, config, func(*string, *string) error { return nil }); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestBaiduDiscoveryAndUnsupportedHTTP(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Method != http.MethodGet || r.URL.Path != "/v2/models" || r.Header.Get("Authorization") != "Bearer fixture" || r.ContentLength > 0 {
			t.Error("Baidu discovery request", r.Method, r.URL.Path)
		}
		fmt.Fprint(w, `{"data":[{"id":"ernie-5.0"}]}`)
	}))
	defer server.Close()
	driver := NewBaiduModel(map[string]string{"default": server.URL + "/v2/"}, URLSuffix{Models: "/models"})
	api := &APIConfig{APIKey: stringPtr("fixture")}
	names, err := driver.ListModels(api)
	if err != nil || !reflect.DeepEqual(names, []string{"ernie-5.0"}) {
		t.Fatal(names, err)
	}
	if err := driver.CheckConnection(api); err != nil || calls.Load() != 2 {
		t.Fatal(err, calls.Load())
	}
	if driver.Name() != "baidu" {
		t.Fatal(driver.Name())
	}
	if _, err := driver.Balance(api); err == nil || calls.Load() != 2 {
		t.Fatal("balance must remain unsupported", err)
	}
}

func TestBaiduRerankTopNHTTP(t *testing.T) {
	for _, config := range []*RerankConfig{nil, {}, {TopN: 2}, {TopN: 99}} {
		t.Run(fmt.Sprint(config), func(t *testing.T) {
			top := 3
			if config != nil && config.TopN == 2 {
				top = 2
			}
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				want := map[string]any{"model": "qwen3-reranker-4b", "query": "q", "documents": []any{"zero", "one", "two"}, "top_n": float64(top)}
				if !reflect.DeepEqual(body, want) || r.URL.Path != "/office/v2/rerank" || r.Method != http.MethodPost || r.Header.Get("Authorization") != "Bearer fixture" {
					t.Error("Baidu rerank request", body, r.URL.Path)
				}
				results := []RerankResult{{Index: 2, RelevanceScore: 0}, {Index: 0, RelevanceScore: 0.9}}
				if top == 3 {
					results = append(results, RerankResult{Index: 1, RelevanceScore: 0.2})
				}
				json.NewEncoder(w).Encode(map[string]any{"results": results})
			}))
			defer server.Close()
			urls := map[string]string{"default": "http://catalog.invalid", "office": server.URL + "/office/v2/"}
			driver := NewBaiduModel(urls, URLSuffix{Rerank: "/rerank"})
			urls["office"] = "http://mutated.invalid"
			api := &APIConfig{APIKey: stringPtr("fixture"), Region: stringPtr("office")}
			name := "qwen3-reranker-4b"
			result, err := driver.Rerank(&name, "q", []string{"zero", "one", "two"}, api, config)
			if err != nil || result == nil || len(result.Data) != top || result.Data[0].Index != 0 || result.Data[top-1].RelevanceScore != 0 || calls.Load() != 1 || api.Context != nil {
				t.Fatal(result, err, calls.Load())
			}
			if top == 3 {
				scores, err := NewRerankModel(driver, &name, api).Rerank("q", []string{"zero", "one", "two"})
				if err != nil || !reflect.DeepEqual(scores, []float64{0.9, 0.2, 0}) {
					t.Fatal("retrieval index mapping", scores, err)
				}
			}
		})
	}
}

func TestBaiduRerankRejectsInvalidHTTP(t *testing.T) {
	for _, body := range []string{
		`{}`, `null`, `{"results":[]}`, `{"results":[{"index":0,"relevance_score":0}]}`,
		`{"results":[{"index":0,"relevance_score":0},{"index":0,"relevance_score":0.1}]}`,
		`{"results":[{"relevance_score":0},{"index":1,"relevance_score":0.1}]}`,
		`{"results":[{"index":0},{"index":1,"relevance_score":0.1}]}`,
		`{"results":[{"index":0,"relevance_score":null},{"index":1,"relevance_score":0.1}]}`,
		`{"results":[{"index":-1,"relevance_score":0},{"index":1,"relevance_score":0.1}]}`,
		`{"results":[{"index":2,"relevance_score":0},{"index":1,"relevance_score":0.1}]}`,
		`{"results":[{"index":0.5,"relevance_score":0},{"index":1,"relevance_score":0.1}]}`,
		`{"results":[{"index":0,"relevance_score":1e999},{"index":1,"relevance_score":0.1}]}`,
		`{"results":[{"index":0,"relevance_score":"NaN"},{"index":1,"relevance_score":0.1}]}`,
		`{"error":"private-body","results":[{"index":0,"relevance_score":0},{"index":1,"relevance_score":0.1}]}`,
		`{"results":[{"index":0,"relevance_score":0},{"index":1,"relevance_score":0.1}]} {}`,
	} {
		t.Run(body, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
			defer server.Close()
			driver := NewBaiduModel(map[string]string{"default": server.URL}, URLSuffix{Rerank: "rerank"})
			result, err := driver.Rerank(stringPtr("reranker"), "q", []string{"a", "b"}, &APIConfig{APIKey: stringPtr("fixture")}, nil)
			if err == nil || result != nil || strings.Contains(err.Error(), "private-body") {
				t.Fatal(result, err)
			}
		})
	}
}

func TestBaiduRerankPreflightAndCancellationHTTP(t *testing.T) {
	started := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		r.Body.Close()
		close(started)
		<-r.Context().Done()
	}))
	defer server.Close()
	driver := NewBaiduModel(map[string]string{"default": server.URL}, URLSuffix{Rerank: "rerank"})
	name, api := stringPtr("reranker"), &APIConfig{APIKey: stringPtr("fixture")}
	if result, err := driver.Rerank(nil, "", nil, nil, nil); err != nil || result == nil || result.Data == nil || len(result.Data) != 0 {
		t.Fatal(result, err)
	}
	if _, err := driver.Rerank(name, "q", []string{"a"}, api, &RerankConfig{TopN: -1}); err == nil {
		t.Fatal("negative top_n accepted")
	}
	if _, err := driver.Rerank(nil, "q", []string{"a"}, api, nil); err == nil {
		t.Fatal("nil model accepted")
	}
	if _, err := driver.Rerank(name, "q", []string{"a"}, nil, nil); err == nil {
		t.Fatal("nil API config accepted")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	api.Context = ctx
	result := make(chan error, 1)
	go func() { _, err := driver.Rerank(name, "q", []string{"a"}, api, nil); result <- err }()
	select {
	case <-started:
		cancel()
	case <-ctx.Done():
		t.Fatal("rerank did not reach HTTP boundary")
	}
	select {
	case err := <-result:
		if !errors.Is(err, context.Canceled) {
			t.Fatal(err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("rerank ignored cancellation")
	}
}
