package models

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
)

func TestVoyageContracts(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("Authorization") != "Bearer fixture" {
			t.Error("auth")
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		switch r.URL.Path {
		case "/v1/embeddings":
			if body["output_dimension"] != float64(2) || body["dimensions"] != nil {
				t.Error("Voyage dimension field", body)
			}
			io.WriteString(w, `{"data":[{"index":1,"embedding":[2,3]},{"index":0,"embedding":[0,1]}]}`)
		case "/v1/rerank":
			if body["top_k"] != float64(1) || body["top_n"] != nil || body["return_documents"] != nil {
				t.Error("Voyage top_k", body)
			}
			io.WriteString(w, `{"data":[{"index":1,"relevance_score":0}]}`)
		default:
			t.Error("unexpected endpoint", r.URL.Path)
		}
	}))
	defer srv.Close()
	d := NewVoyageModel(map[string]string{"default": srv.URL + "/"}, URLSuffix{Embedding: "v1/embeddings", Rerank: "v1/rerank"})
	name, key := "voyage-3.5", "fixture"
	api := &APIConfig{APIKey: &key}
	v, err := d.Embed(&name, []string{"a", "b"}, api, &EmbeddingConfig{Dimension: 2})
	if err != nil || len(v) != 2 || v[0].Embedding[0] != 0 {
		t.Fatalf("%v %v", v, err)
	}
	result, err := d.Rerank(&name, "q", []string{"a", "b"}, api, &RerankConfig{TopN: 1})
	if err != nil || len(result.Data) != 1 || result.Data[0].Index != 1 || result.Data[0].RelevanceScore != 0 {
		t.Fatalf("%v %v", result, err)
	}
	before := calls.Load()
	if _, err := d.ListModels(api); !errors.Is(err, ErrUnsupportedCapability) {
		t.Fatal(err)
	}
	if err := d.CheckConnection(api); !errors.Is(err, ErrUnsupportedCapability) {
		t.Fatal(err)
	}
	if _, err := d.Balance(api); !errors.Is(err, ErrUnsupportedCapability) {
		t.Fatal(err)
	}
	if _, err := d.ChatWithMessages(name, api, []Message{{Role: "user", Content: "x"}}, nil); err == nil {
		t.Fatal("chat success")
	}
	if _, ok := any(d).(OCRDriver); ok {
		t.Fatal("unexpected OCR")
	}
	if _, err := d.Embed(nil, nil, nil, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := d.Rerank(nil, "", nil, nil, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := d.Embed(&name, []string{"x"}, nil, nil); err == nil {
		t.Fatal("key missing")
	}
	if _, err := d.Rerank(&name, "q", []string{"x"}, nil, nil); err == nil {
		t.Fatal("key missing")
	}
	if _, err := d.Rerank(&name, "q", []string{"x"}, api, &RerankConfig{TopN: -1}); err == nil {
		t.Fatal("negative top")
	}
	region := "unknown"
	if _, err := d.Embed(&name, []string{"x"}, &APIConfig{APIKey: &key, Region: &region}, nil); err == nil {
		t.Fatal("unknown region")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := d.Rerank(&name, "q", []string{"x"}, &APIConfig{APIKey: &key, Context: ctx}, nil); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if calls.Load() != before {
		t.Fatal("unsupported or invalid call consumed quota")
	}
}
func TestVoyageStrictResults(t *testing.T) {
	for _, body := range []string{`{"data":[{"index":0}]}`, `{"data":[{"index":null,"relevance_score":0}]}`, `{"data":[{"index":0,"relevance_score":null}]}`, `{"data":[{"index":1,"relevance_score":0}]}`, `{"data":[{"index":0,"relevance_score":0},{"index":0,"relevance_score":1}]}`, `{"data":[]}`, `{"data":[{"index":0,"relevance_score":1e999}]}`, `{"results":[{"index":0,"relevance_score":0}]}`} {
		t.Run(body, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, body) }))
			defer srv.Close()
			d := NewVoyageModel(map[string]string{"default": srv.URL}, URLSuffix{Rerank: "rerank"})
			name, key := "m", "fixture"
			if _, err := d.Rerank(&name, "q", []string{"x"}, &APIConfig{APIKey: &key}, nil); err == nil {
				t.Fatal("malformed rerank accepted")
			}
		})
	}
}
func TestVoyageConstructorCustomDefaultTransport(t *testing.T) {
	old := http.DefaultTransport
	http.DefaultTransport = customProviderTransport{}
	defer func() { http.DefaultTransport = old }()
	d := NewVoyageModel(nil, URLSuffix{})
	if d.Name() != "Voyage AI" {
		t.Fatal("canonical name")
	}
	providerHTTPTransport().CloseIdleConnections()
}

func TestVoyageStrictEmbeddings(t *testing.T) {
	for _, body := range []string{
		`{"data":[{"embedding":[1]}]}`,
		`{"data":[{"index":null,"embedding":[1]}]}`,
		`{"data":[{"index":0,"embedding":[null]}]}`,
		`{"data":[{"index":0,"embedding":[1,2]}]}`,
		`{"data":[{"index":1,"embedding":[1]}]}`,
		`{"data":[{"index":0,"embedding":[1e999]}]}`,
		`{"data":[]}`,
		`{"data":[{"index":0,"embedding":[1]}],"error":"failed"}`,
	} {
		t.Run(body, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, body) }))
			defer srv.Close()
			d := NewVoyageModel(map[string]string{"default": srv.URL}, URLSuffix{Embedding: "embeddings"})
			name, key := "m", "fixture"
			if _, err := d.Embed(&name, []string{"x"}, &APIConfig{APIKey: &key}, &EmbeddingConfig{Dimension: 1}); err == nil {
				t.Fatal("malformed embedding accepted")
			}
		})
	}
}
func TestVoyageBatchDimensionsAndDefaultTopK(t *testing.T) {
	embeddingCalls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		if r.URL.Path == "/embeddings" {
			embeddingCalls++
			if body["output_dimension"] != float64(1) || body["dimensions"] != nil || body["query"] != nil {
				t.Error("batch dimension/metadata", body)
			}
			inputs := body["input"].([]any)
			want := 32
			if embeddingCalls == 2 {
				want = 1
			}
			if len(inputs) != want {
				t.Error("batch size", len(inputs))
			}
			items := []map[string]any{}
			for i := len(inputs) - 1; i >= 0; i-- {
				items = append(items, map[string]any{"index": i, "embedding": []float64{1}})
			}
			json.NewEncoder(w).Encode(map[string]any{"data": items})
			return
		}
		if body["top_k"] != float64(2) || body["top_n"] != nil {
			t.Error("default/clamped top_k", body)
		}
		io.WriteString(w, `{"data":[{"index":1,"relevance_score":1},{"index":0,"relevance_score":0}]}`)
	}))
	defer srv.Close()
	d := NewVoyageModel(map[string]string{"default": srv.URL}, URLSuffix{Embedding: "embeddings", Rerank: "rerank"})
	name, key := "m", "fixture"
	api := &APIConfig{APIKey: &key}
	texts := make([]string, 33)
	for i := range texts {
		texts[i] = "x"
	}
	result, err := d.Embed(&name, texts, api, &EmbeddingConfig{Dimension: 1, Query: true})
	if err != nil || len(result) != 33 || result[32].Index != 32 || embeddingCalls != 2 {
		t.Fatalf("batch %v %v", result, err)
	}
	for _, config := range []*RerankConfig{nil, {TopN: 0}, {TopN: 99}} {
		result, err := d.Rerank(&name, "q", []string{"a", "b"}, api, config)
		if err != nil || len(result.Data) != 2 {
			t.Fatalf("default top %v %v", result, err)
		}
	}
}
