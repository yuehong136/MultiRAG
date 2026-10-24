package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

type after2836NumericProvider struct {
	name, baseSuffix, embedding, rerank, key string
}

func after2836NumericProviders() []after2836NumericProvider {
	return []after2836NumericProvider{
		{"TogetherAI", "/v1", "embeddings", "", "key"},
		{"Xinference", "/v1/", "v1/embeddings", "v1/rerank", ""},
		{"Xinference", "", "v1/embeddings", "v1/rerank", "key"},
		{"Novita", "", "", "openai/v1/rerank", "key"},
	}
}

func (p after2836NumericProvider) driver(base string) ModelDriver {
	driver, _ := NewModelFactory().CreateModelDriver(p.name, map[string]string{"default": base + p.baseSuffix}, URLSuffix{Embedding: p.embedding, Rerank: p.rerank})
	return driver
}

func (p after2836NumericProvider) path(suffix string) string {
	if p.name == "TogetherAI" {
		return "/v1/" + suffix
	}
	return "/" + suffix
}

func (p after2836NumericProvider) auth() string {
	if p.key == "" {
		return ""
	}
	return "Bearer " + p.key
}

func TestAfter2836EmbeddingHTTPAndBatches(t *testing.T) {
	for _, p := range after2836NumericProviders() {
		if p.embedding == "" {
			continue
		}
		t.Run(p.name+"/key="+p.key, func(t *testing.T) {
			var requests atomic.Int32
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				requests.Add(1)
				var body struct {
					Model      string   `json:"model"`
					Input      []string `json:"input"`
					Dimensions int      `json:"dimensions"`
					Query      any      `json:"query"`
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if r.Method != http.MethodPost || r.URL.Path != p.path(p.embedding) || r.Header.Get("Authorization") != p.auth() || body.Model != "served" || body.Dimensions != 2 || body.Query != nil || len(body.Input) > 32 {
					t.Error("embedding endpoint, key or dimensions lost", r.URL, body)
				}
				data := []map[string]any{}
				for i := len(body.Input) - 1; i >= 0; i-- {
					data = append(data, map[string]any{"index": i, "embedding": []float64{float64(len(body.Input[i])), 0}})
				}
				json.NewEncoder(w).Encode(map[string]any{"data": data})
			}))
			defer srv.Close()
			driver := p.driver(srv.URL)
			texts := make([]string, 33)
			for i := range texts {
				texts[i] = strings.Repeat("x", i+1)
			}
			result, err := driver.Embed(stringPtr("served"), texts, &APIConfig{APIKey: &p.key}, &EmbeddingConfig{Query: true, Dimension: 2})
			if err != nil || len(result) != 33 || requests.Load() != 2 {
				t.Fatal(result, err, requests.Load())
			}
			for i, data := range result {
				if data.Index != i || len(data.Embedding) != 2 || data.Embedding[0] != float64(i+1) || data.Embedding[1] != 0 {
					t.Fatal("indexed reconstruction or legal zero lost", data)
				}
			}
			if result, err := driver.Embed(nil, nil, nil, nil); err != nil || result == nil || len(result) != 0 || requests.Load() != 2 {
				t.Fatal("empty inputs must be empty without HTTP", result, err)
			}
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			if _, err := driver.Embed(stringPtr("served"), []string{"x"}, &APIConfig{APIKey: &p.key, Context: ctx}, nil); !errors.Is(err, context.Canceled) {
				t.Fatal("cancellation lost", err)
			}
		})
	}
}

func TestAfter2836EmbeddingRejectsMalformed(t *testing.T) {
	responses := []string{
		`{"data":[{"index":0,"embedding":[0,1]},{"index":0,"embedding":[0,1]}]}`,
		`{"data":[{"embedding":[0,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":null,"embedding":[0,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":-1,"embedding":[0,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":2,"embedding":[0,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0,"embedding":[]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0,"embedding":null},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0,"embedding":[null,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0,"embedding":[true,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0,"embedding":[1e400,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0,"embedding":[1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0,"embedding":[1]},{"index":1,"embedding":[0]}]}`,
		`{"data":[{"index":0,"embedding":[0,1]}]}`, `{"data":null}`, `{}`, `null`,
		`{"data":[],"error":"private-secret"}`, `{} {}`, `{"data":[`,
	}
	for _, p := range after2836NumericProviders() {
		if p.embedding == "" {
			continue
		}
		for i, response := range responses {
			t.Run(fmt.Sprintf("%s/key=%s/%d", p.name, p.key, i), func(t *testing.T) {
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, response) }))
				defer srv.Close()
				if _, err := p.driver(srv.URL).Embed(stringPtr("served"), []string{"x", "y"}, &APIConfig{APIKey: &p.key}, &EmbeddingConfig{Dimension: 2}); err == nil || strings.Contains(err.Error(), "private-secret") {
					t.Fatal("invalid vectors accepted or private body leaked", err)
				}
			})
		}
	}
}

func TestAfter2836RerankHTTPAndTopN(t *testing.T) {
	for _, p := range after2836NumericProviders() {
		if p.rerank == "" {
			continue
		}
		for _, config := range []*RerankConfig{nil, {}, {TopN: 1}, {TopN: 10}} {
			t.Run(p.name+"/key="+p.key+fmt.Sprint(config), func(t *testing.T) {
				var requests atomic.Int32
				top, _ := RerankTopN(2, config)
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					requests.Add(1)
					var body map[string]any
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
					}
					if r.Method != http.MethodPost || r.URL.Path != p.path(p.rerank) || r.Header.Get("Authorization") != p.auth() || body["top_n"] != float64(top) || body["model"] != "served" || body["query"] != "q" || body["return_documents"] != nil {
						t.Error("rerank endpoint, key or payload lost", r.URL, body)
					}
					if top == 1 {
						fmt.Fprint(w, `{"results":[{"index":1,"relevance_score":0}]}`)
					} else {
						fmt.Fprint(w, `{"results":[{"index":1,"relevance_score":0},{"index":0,"relevance_score":0.9}]}`)
					}
				}))
				defer srv.Close()
				driver := p.driver(srv.URL)
				result, err := driver.Rerank(stringPtr("served"), "q", []string{"x", "y"}, &APIConfig{APIKey: &p.key}, config)
				if err != nil || len(result.Data) != top || result.Data[top-1].Index != 1 || result.Data[top-1].RelevanceScore != 0 || top == 2 && result.Data[0].Index != 0 {
					t.Fatal("top_n, zero or sorting lost", result, err)
				}
				if result, err := driver.Rerank(nil, "", nil, nil, nil); err != nil || result == nil || result.Data == nil || len(result.Data) != 0 || requests.Load() != 1 {
					t.Fatal("empty inputs invoked provider", result, err)
				}
				if _, err := driver.Rerank(stringPtr("served"), "q", []string{"x"}, &APIConfig{APIKey: &p.key}, &RerankConfig{TopN: -1}); err == nil || requests.Load() != 1 {
					t.Fatal("negative top_n accepted", err)
				}
				ctx, cancel := context.WithCancel(context.Background())
				cancel()
				if _, err := driver.Rerank(stringPtr("served"), "q", []string{"x"}, &APIConfig{APIKey: &p.key, Context: ctx}, nil); !errors.Is(err, context.Canceled) {
					t.Fatal("cancellation lost", err)
				}
			})
		}
	}
}

func TestAfter2836RerankRejectsMalformed(t *testing.T) {
	responses := []string{
		`{"results":[{"relevance_score":0},{"index":1,"relevance_score":1}]}`,
		`{"results":[{"index":null,"relevance_score":0},{"index":1,"relevance_score":1}]}`,
		`{"results":[{"index":0},{"index":1,"relevance_score":1}]}`,
		`{"results":[{"index":0,"relevance_score":null},{"index":1,"relevance_score":1}]}`,
		`{"results":[{"index":0,"relevance_score":0},{"index":0,"relevance_score":1}]}`,
		`{"results":[{"index":-1,"relevance_score":0},{"index":1,"relevance_score":1}]}`,
		`{"results":[{"index":2,"relevance_score":0},{"index":1,"relevance_score":1}]}`,
		`{"results":[{"index":0,"relevance_score":1e400},{"index":1,"relevance_score":1}]}`,
		`{"results":[{"index":0,"relevance_score":0}]}`, `{"results":[]}`, `{"results":null}`,
		`{}`, `null`, `{"error":"private-secret"}`, `{} {}`, `{"results":[`,
	}
	for _, p := range after2836NumericProviders() {
		if p.rerank == "" {
			continue
		}
		for i, response := range responses {
			t.Run(fmt.Sprintf("%s/key=%s/%d", p.name, p.key, i), func(t *testing.T) {
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, response) }))
				defer srv.Close()
				if _, err := p.driver(srv.URL).Rerank(stringPtr("served"), "q", []string{"x", "y"}, &APIConfig{APIKey: &p.key}, nil); err == nil || strings.Contains(err.Error(), "private-secret") {
					t.Fatal("invalid scores accepted or private body leaked", err)
				}
			})
		}
	}
}
