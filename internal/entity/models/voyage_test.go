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
	d.httpClient.CloseIdleConnections()
}
