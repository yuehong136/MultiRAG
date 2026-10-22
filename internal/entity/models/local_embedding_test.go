package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strconv"
	"sync/atomic"
	"testing"
)

func localEmbeddingProviders() []string { return []string{"vllm", "ollama", "LM-Studio"} }

func localEmbeddingDriver(t *testing.T, provider string, urls map[string]string) ModelDriver {
	t.Helper()
	suffix := "embeddings"
	if provider == "ollama" {
		suffix = "v1/embeddings"
	}
	driver, err := NewModelFactory().CreateModelDriver(provider, urls, URLSuffix{Embedding: suffix})
	if err != nil {
		t.Fatal(err)
	}
	return driver
}

func TestLocalEmbeddingKeylessBatchesRegionAndDimensionHTTP(t *testing.T) {
	for _, provider := range localEmbeddingProviders() {
		t.Run(provider, func(t *testing.T) {
			for _, count := range []int{0, 1, 32, 33, 65} {
				t.Run(strconv.Itoa(count), func(t *testing.T) {
					var batches []int
					server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						path := "/instance/embeddings"
						if provider == "ollama" {
							path = "/instance/v1/embeddings"
						}
						if r.Method != "POST" || r.URL.Path != path || r.Header.Get("Authorization") != "" {
							t.Error(r.Method, r.URL.Path, "unexpected auth")
						}
						var body struct {
							Model      string
							Input      []string
							Dimensions int
							Query      *bool
						}
						if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
							t.Error(err)
						}
						if body.Model != "embed" || body.Dimensions != 2 || body.Query != nil {
							t.Error(body)
						}
						batches = append(batches, len(body.Input))
						data := make([]map[string]any, 0, len(body.Input))
						for i := len(body.Input) - 1; i >= 0; i-- {
							value, err := strconv.Atoi(body.Input[i])
							if err != nil {
								t.Error(err)
							}
							data = append(data, map[string]any{"index": i, "embedding": []float64{0, float64(value)}})
						}
						json.NewEncoder(w).Encode(map[string]any{"data": data})
					}))
					defer server.Close()
					driver := localEmbeddingDriver(t, provider, map[string]string{"default": server.URL + "/instance/"})
					name, unknown := "embed", "unknown"
					api := &APIConfig{Region: &unknown}
					cfg := &EmbeddingConfig{Dimension: 2, Query: true}
					texts := make([]string, count)
					for i := range texts {
						texts[i] = strconv.Itoa(i)
					}
					vectors, err := driver.Encode(&name, texts, api, cfg)
					if err != nil || vectors == nil || len(vectors) != count {
						t.Fatal(vectors, err)
					}
					for i, v := range vectors {
						if !reflect.DeepEqual(v, []float64{0, float64(i)}) {
							t.Fatal(i, v)
						}
					}
					want := []int(nil)
					for remaining := count; remaining > 0; remaining -= 32 {
						want = append(want, min(32, remaining))
					}
					if !reflect.DeepEqual(batches, want) || !cfg.Query || unknown != "unknown" {
						t.Fatal(batches, want, "configuration mutation")
					}
				})
			}
		})
	}
}

func TestLocalEmbeddingRejectsMalformedHTTP(t *testing.T) {
	responses := []string{
		`{"data":[]}`, `{"data":null}`, `{}`,
		`{"data":[{"index":0,"embedding":[1,2]}]}`,
		`{"data":[{"index":0,"embedding":[1,2]},{"index":0,"embedding":[1,2]}]}`,
		`{"data":[{"index":0,"embedding":[1,2]},{"index":2,"embedding":[1,2]}]}`,
		`{"data":[{"index":-1,"embedding":[1,2]},{"index":1,"embedding":[1,2]}]}`,
		`{"data":[{"index":null,"embedding":[1,2]},{"index":1,"embedding":[1,2]}]}`,
		`{"data":[{"embedding":[1,2]},{"index":1,"embedding":[1,2]}]}`,
		`{"data":[{"index":0,"embedding":[]},{"index":1,"embedding":[1,2]}]}`,
		`{"data":[{"index":0,"embedding":[null,2]},{"index":1,"embedding":[1,2]}]}`,
		`{"data":[{"index":0,"embedding":[1,2]},{"index":1,"embedding":[1]}]}`,
		`{"data":[{"index":0,"embedding":[1,2]},{"index":1,"embedding":[1,1e400]}]}`,
		`{"data":[{"index":0,"embedding":[1,2]},{"index":1,"embedding":[1,"2"]}]}`,
		`{"data":[{"index":0,"embedding":[1,2]},{"index":1,"embedding":[1,2]}]} {}`,
		`{"error":"failed","data":[{"index":0,"embedding":[1,2]},{"index":1,"embedding":[1,2]}]}`,
	}
	for _, provider := range localEmbeddingProviders() {
		t.Run(provider, func(t *testing.T) {
			for i, response := range responses {
				t.Run(strconv.Itoa(i), func(t *testing.T) {
					server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, response) }))
					defer server.Close()
					driver := localEmbeddingDriver(t, provider, map[string]string{"default": server.URL})
					if vectors, err := driver.Encode(stringPtr("embed"), []string{"a", "b"}, nil, &EmbeddingConfig{Dimension: 2}); err == nil || vectors != nil {
						t.Fatal("malformed response accepted", vectors, err)
					}
				})
			}
		})
	}
}

func TestLocalEmbeddingPreflightAuthAndCancellationHTTP(t *testing.T) {
	for _, provider := range localEmbeddingProviders() {
		t.Run(provider, func(t *testing.T) {
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.Header.Get("Authorization") != "Bearer optional" {
					t.Error("provided key missing")
				}
				fmt.Fprint(w, `{"data":[{"index":0,"embedding":[0,2]}]}`)
			}))
			defer server.Close()
			driver := localEmbeddingDriver(t, provider, map[string]string{"default": server.URL, "office": server.URL})
			name, key, region := "embed", "optional", "office"
			if got, err := driver.Encode(&name, []string{"a"}, &APIConfig{APIKey: &key, Region: &region}, nil); err != nil || len(got) != 1 {
				t.Fatal(got, err)
			}
			for _, invalid := range []struct {
				name *string
				cfg  *EmbeddingConfig
			}{{nil, nil}, {stringPtr(" "), nil}, {&name, &EmbeddingConfig{Dimension: -1}}, {&name, &EmbeddingConfig{Dimension: 3}}} {
				if got, err := driver.Encode(invalid.name, []string{"a"}, &APIConfig{APIKey: &key}, invalid.cfg); err == nil || got != nil {
					t.Fatal("invalid config/response accepted")
				}
			}
			if calls.Load() != 2 {
				t.Fatal("invalid preflight reached provider", calls.Load())
			}
			for _, urls := range []map[string]string{nil, {"office": ""}} {
				if got, err := localEmbeddingDriver(t, provider, urls).Encode(&name, []string{"a"}, nil, nil); err == nil || got != nil {
					t.Fatal("missing URL accepted")
				}
			}
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			if got, err := driver.Encode(&name, []string{"a"}, &APIConfig{Context: ctx}, nil); !errors.Is(err, context.Canceled) || got != nil {
				t.Fatal(got, err)
			}
			if calls.Load() != 2 {
				t.Fatal("cancel reached provider")
			}
		})
	}
}

func TestLocalEmbeddingLaterBatchFailureHTTP(t *testing.T) {
	for _, provider := range localEmbeddingProviders() {
		t.Run(provider, func(t *testing.T) {
			for _, status := range []int{http.StatusOK, http.StatusServiceUnavailable} {
				t.Run(strconv.Itoa(status), func(t *testing.T) {
					calls := 0
					server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						calls++
						var body struct{ Input []string }
						json.NewDecoder(r.Body).Decode(&body)
						if calls == 2 && status != http.StatusOK {
							w.WriteHeader(status)
							fmt.Fprint(w, "private failure")
							return
						}
						dim := 2
						if calls == 2 {
							dim = 3
						}
						data := make([]map[string]any, len(body.Input))
						for i := range data {
							data[i] = map[string]any{"index": i, "embedding": make([]float64, dim)}
						}
						json.NewEncoder(w).Encode(map[string]any{"data": data})
					}))
					defer server.Close()
					got, err := localEmbeddingDriver(t, provider, map[string]string{"default": server.URL}).Encode(stringPtr("embed"), make([]string, 33), nil, nil)
					if err == nil || got != nil || calls != 2 {
						t.Fatal("partial success", len(got), err, calls)
					}
				})
			}
		})
	}
}

func TestLocalEmbeddingCancellationDuringHTTP(t *testing.T) {
	for _, provider := range localEmbeddingProviders() {
		t.Run(provider, func(t *testing.T) {
			started := make(chan struct{})
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				close(started)
				<-r.Context().Done()
			}))
			defer server.Close()
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			go func() { <-started; cancel() }()
			got, err := localEmbeddingDriver(t, provider, map[string]string{"default": server.URL}).Encode(stringPtr("embed"), []string{"a"}, &APIConfig{Context: ctx}, nil)
			if !errors.Is(err, context.Canceled) || got != nil {
				t.Fatal(got, err)
			}
		})
	}
}
