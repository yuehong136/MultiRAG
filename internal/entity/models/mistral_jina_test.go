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
)

func TestMistralEmbeddingsHTTP(t *testing.T) { testHostedEmbeddingsHTTP(t, "Mistral") }
func TestJinaEmbeddingsHTTP(t *testing.T)    { testHostedEmbeddingsHTTP(t, "Jina") }

func TestMistralChatStructuredContentHTTP(t *testing.T) {
	var body map[string]any
	var response = `{"choices":[{"message":{"content":[{"type":"thinking","thinking":[{"type":"text","text":"reason"}]},{"type":"citation"},{"type":"text","text":"answer"}]}}]}`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer key" || r.URL.Path != "/v1/chat/completions" {
			t.Error("endpoint or key")
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		fmt.Fprint(w, response)
	}))
	defer server.Close()
	d := NewMistralModel(map[string]string{"default": server.URL + "/v1/"}, URLSuffix{Chat: "chat/completions"})
	messages := []Message{{Role: "system", Content: "rules"}, {Role: "assistant", Content: "previous"}, {Role: "user", Content: []any{map[string]any{"type": "image_url", "image_url": map[string]any{"url": "https://example.com/image.png"}}}}}
	yes := true
	result, err := d.ChatWithMessages("pixtral", &APIConfig{APIKey: stringPtr("key")}, messages, &ChatConfig{Stream: &yes})
	if err != nil || *result.Answer != "answer" || *result.ReasoningContent != "reason" || body["stream"] != false || len(body["messages"].([]any)) != 3 {
		t.Fatal(result, err, body)
	}
	for _, invalid := range []string{`{"choices":[]}`, `{"error":{"message":"private-body"}}`, `{"choices":[{"message":{"content":[]}}]}`, `{"choices":[{"message":{"content":false}}]}`, `{"choices":[{"message":{"content":"answer"}}]} {}`} {
		response = invalid
		if r, e := d.ChatWithMessages("pixtral", &APIConfig{APIKey: stringPtr("key")}, messages, nil); e == nil || r != nil || strings.Contains(e.Error(), "private-body") {
			t.Fatal(r, e)
		}
	}
}

func TestMistralStreamHTTP(t *testing.T) {
	for _, test := range []struct {
		name, events string
		valid        bool
	}{
		{"structured", `data: {"choices":[{"delta":{"content":[{"type":"thinking","thinking":[{"type":"text","text":"reason"}]},{"type":"text","text":"answer"}]},"finish_reason":"stop"}]}` + "\n\n", true},
		{"done", "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n", true},
		{"empty", "data: [DONE]\n\n", false},
		{"truncated", "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\n", false},
		{"malformed", "data: invalid\n\ndata: [DONE]\n\n", false},
		{"late error", "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"},\"finish_reason\":\"stop\"}]}\n\ndata: {\"error\":{\"message\":\"private-body\"}}\n\n", false},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, test.events) }))
			defer server.Close()
			d := NewMistralModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
			done := 0
			err := d.ChatStreamlyWithMessages("model", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: stringPtr("key")}, nil, func(a, r *string) error {
				if a != nil && *a == "[DONE]" {
					done++
				}
				return nil
			})
			if (err == nil) != test.valid || test.valid && done != 1 || !test.valid && done != 0 {
				t.Fatal(err, done)
			}
		})
	}
}

func TestJinaTaskDiscoveryRerankHTTP(t *testing.T) {
	var response string
	var calls atomic.Int32
	var task string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Method == http.MethodGet {
			if r.Header.Get("Authorization") != "" {
				t.Error("public discovery unexpectedly keyed")
			}
			fmt.Fprint(w, response)
			return
		}
		var body map[string]any
		if e := json.NewDecoder(r.Body).Decode(&body); e != nil {
			t.Error(e)
		}
		if r.Header.Get("Authorization") != "Bearer key" {
			t.Error("key missing")
		}
		if r.URL.Path == "/embeddings" {
			task, _ = body["task"].(string)
			if body["dimensions"] != float64(2) || body["query"] != nil {
				t.Error(body)
			}
			fmt.Fprint(w, `{"data":[{"index":0,"embedding":[0,1]}]}`)
			return
		}
		if body["top_n"] != float64(2) || body["return_documents"] != false {
			t.Error(body)
		}
		fmt.Fprint(w, response)
	}))
	defer server.Close()
	d := NewJinaModel(map[string]string{"default": server.URL}, URLSuffix{Models: "models", Embedding: "embeddings", Rerank: "rerank", Chat: "chat"})
	for _, q := range []bool{false, true} {
		_, e := d.Embed(stringPtr("jina-embeddings-v3"), []string{"q"}, &APIConfig{APIKey: stringPtr("key")}, &EmbeddingConfig{Query: q, Dimension: 2})
		want := "retrieval.passage"
		if q {
			want = "retrieval.query"
		}
		if e != nil || task != want {
			t.Fatal(e, task)
		}
	}
	response = `{"data":[{"id":"model-id","name":"Display label"}]}`
	list, e := d.ListModels(nil)
	if e != nil || !reflect.DeepEqual(list, []string{"model-id"}) {
		t.Fatal(list, e)
	}
	for _, responseBody := range []string{`{}`, `{"data":null}`, `{"data":[{"name":"display"}]}`, `{"data":[1]}`, `{"error":{"message":"private-body"}}`} {
		response = responseBody
		if _, e := d.ListModels(nil); e == nil || strings.Contains(e.Error(), "private-body") {
			t.Fatal(e)
		}
	}
	response = `{"results":[{"index":1,"relevance_score":1},{"index":0,"relevance_score":0}]}`
	name := "reranker"
	api := &APIConfig{APIKey: stringPtr("key")}
	for _, cfg := range []*RerankConfig{nil, {}, {TopN: 99}} {
		r, e := d.Rerank(&name, "q", []string{"a", "b"}, api, cfg)
		if e != nil || len(r.Data) != 2 || r.Data[1].RelevanceScore != 0 {
			t.Fatal(r, e)
		}
	}
	for _, bad := range []string{`{"results":[]}`, `{"results":[{"index":0,"relevance_score":0},{"index":0,"relevance_score":1}]}`, `{"results":[{"index":0,"relevance_score":null},{"index":1,"relevance_score":1}]}`, `{"results":[{"relevance_score":0},{"index":1,"relevance_score":1}]}`, `{"results":[{"index":0,"relevance_score":1e999},{"index":1,"relevance_score":1}]}`, `{"results":[{"index":-1,"relevance_score":0},{"index":1,"relevance_score":1}]}`} {
		response = bad
		if r, e := d.Rerank(&name, "q", []string{"a", "b"}, api, nil); e == nil || r != nil {
			t.Fatal(r, e)
		}
	}
	before := calls.Load()
	if _, e := d.Rerank(&name, "q", []string{"a"}, api, &RerankConfig{TopN: -1}); e == nil {
		t.Fatal("negative top_n")
	}
	if _, e := d.ChatWithMessages("chat", api, []Message{{Role: "user", Content: "q"}}, nil); e == nil {
		t.Fatal("unsupported chat")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, e := d.ListModels(&APIConfig{Context: ctx}); !errors.Is(e, context.Canceled) {
		t.Fatal(e)
	}
	if calls.Load() != before {
		t.Fatal("rejected operation reached supplier")
	}
}

func TestMistralNativeEmbeddingDimensionHTTP(t *testing.T) {
	var calls atomic.Int32
	fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body["output_dimension"] != float64(2) || body["dimensions"] != nil || body["query"] != nil {
			t.Error("native dimensions lost", body)
		}
		fmt.Fprint(w, `{"data":[{"index":0,"embedding":[0,1]}]}`)
	}))
	defer fixture.Close()
	d := NewMistralModel(map[string]string{"default": fixture.URL}, URLSuffix{Embedding: "embeddings"})
	api := &APIConfig{APIKey: stringPtr("key")}
	if rows, err := d.Embed(stringPtr("mistral-embed"), []string{"q"}, api, &EmbeddingConfig{Query: true, Dimension: 2}); err != nil || len(rows) != 1 {
		t.Fatal(rows, err)
	}
	if rows, err := d.Embed(stringPtr("mistral-embed"), []string{"q"}, api, &EmbeddingConfig{Dimension: -1}); err == nil || rows != nil || calls.Load() != 1 {
		t.Fatal(rows, err, calls.Load())
	}
}
