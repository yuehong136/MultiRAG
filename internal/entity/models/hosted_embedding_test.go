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
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestBaiduEmbeddingsHTTP(t *testing.T)  { testHostedEmbeddingsHTTP(t, "Baidu") }
func TestOpenAIEmbeddingsHTTP(t *testing.T) { testHostedEmbeddingsHTTP(t, "OpenAI") }

func TestHostedEmbeddingDimensionsHTTP(t *testing.T) {
	for _, provider := range []string{"OpenAI", "Baidu"} {
		t.Run(provider, func(t *testing.T) {
			for _, config := range []*EmbeddingConfig{nil, {}, {Query: true, Dimension: 2}} {
				t.Run(fmt.Sprint(config), func(t *testing.T) {
					var calls atomic.Int32
					server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						calls.Add(1)
						var body map[string]any
						if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
							t.Error(err)
						}
						if config != nil && config.Dimension == 2 {
							if body["dimensions"] != float64(2) {
								t.Error("requested dimensions missing", body)
							}
						} else if _, exists := body["dimensions"]; exists {
							t.Error("default dimensions should be omitted", body)
						}
						if _, exists := body["query"]; exists {
							t.Error("local Query flag leaked into provider payload")
						}
						inputs, ok := body["input"].([]any)
						if !ok {
							t.Error("input must be an array")
						}
						data := make([]map[string]any, len(inputs))
						for i := range data {
							data[i] = map[string]any{"index": i, "embedding": []float64{0, 1}}
						}
						json.NewEncoder(w).Encode(map[string]any{"data": data})
					}))
					defer server.Close()
					driver, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings"})
					vectors, err := driver.Encode(stringPtr("text-embedding"), make([]string, 33), &APIConfig{APIKey: stringPtr("fixture")}, config)
					if err != nil || len(vectors) != 33 || calls.Load() != 2 || config != nil && config.Dimension == 2 && !config.Query {
						t.Fatal(vectors, err, calls.Load())
					}
				})
			}
			t.Run("requested dimension mismatch and negative dimension", func(t *testing.T) {
				var calls atomic.Int32
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					calls.Add(1)
					fmt.Fprint(w, `{"data":[{"index":0,"embedding":[0]}]}`)
				}))
				defer server.Close()
				driver, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings"})
				api := &APIConfig{APIKey: stringPtr("fixture")}
				for _, dimension := range []int{-1, 2} {
					vectors, err := driver.Encode(stringPtr("model"), []string{"a"}, api, &EmbeddingConfig{Dimension: dimension})
					if err == nil || vectors != nil {
						t.Fatal("invalid dimension yielded vectors", dimension, vectors, err)
					}
				}
				if calls.Load() != 1 {
					t.Fatal("negative dimension reached provider", calls.Load())
				}
			})
		})
	}
}

func testHostedEmbeddingsHTTP(t *testing.T, provider string) {
	t.Helper()
	t.Run("ordered batches and isolated instances", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				t.Parallel()
				var calls atomic.Int32
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					calls.Add(1)
					var body struct {
						Model string   `json:"model"`
						Input []string `json:"input"`
					}
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
					}
					if body.Model != instance || len(body.Input) > 32 || r.Method != http.MethodPost || r.URL.Path != "/v2/embeddings" || r.Header.Get("Authorization") != "Bearer "+instance || r.Header.Get("Content-Type") != "application/json" {
						t.Error("embedding payload/auth/instance isolation")
					}
					data := make([]map[string]any, 0, len(body.Input))
					for i := len(body.Input) - 1; i >= 0; i-- {
						value, err := strconv.Atoi(body.Input[i])
						if err != nil {
							t.Error(err)
						}
						data = append(data, map[string]any{"index": i, "embedding": []float64{float64(value), 0}})
					}
					json.NewEncoder(w).Encode(map[string]any{"data": data})
				}))
				defer server.Close()
				urls := map[string]string{"default": "http://catalog.invalid", "office": server.URL + "/v2/"}
				driver, err := NewModelFactory().CreateModelDriver(provider, urls, URLSuffix{Embedding: "embeddings"})
				if err != nil {
					t.Fatal(err)
				}
				urls["office"] = "http://mutated.invalid"
				api := &APIConfig{APIKey: &instance, Region: stringPtr("office")}
				model := NewEmbeddingModel(driver, &instance, api)
				model.EmbeddingConfig = &EmbeddingConfig{Query: true}
				if vectors, err := model.Encode(nil); err != nil || vectors == nil || len(vectors) != 0 || calls.Load() != 0 {
					t.Fatal(vectors, err, calls.Load())
				}
				texts := make([]string, 33)
				want := make([][]float64, 33)
				for i := range texts {
					texts[i], want[i] = strconv.Itoa(i), []float64{float64(i), 0}
				}
				vectors, err := model.Encode(texts)
				if err != nil || !reflect.DeepEqual(vectors, want) || calls.Load() != 2 || api.Context != nil || !model.EmbeddingConfig.Query {
					t.Fatal(vectors, err, calls.Load())
				}
			})
		}
	})
	t.Run("reject invalid responses", func(t *testing.T) {
		for _, body := range []string{
			``, `bad`, `null`, `{}`, `{"data":null}`, `{"data":[]}`,
			`{"data":[{"index":0,"embedding":[1,0]}]}`,
			`{"data":[{"index":0,"embedding":[1]},{"index":0,"embedding":[2]}]}`,
			`{"data":[{"embedding":[1]},{"index":1,"embedding":[2]}]}`,
			`{"data":[{"index":null,"embedding":[1]},{"index":1,"embedding":[2]}]}`,
			`{"data":[{"index":0.5,"embedding":[1]},{"index":1,"embedding":[2]}]}`,
			`{"data":[{"index":"0","embedding":[1]},{"index":1,"embedding":[2]}]}`,
			`{"data":[{"index":-1,"embedding":[1]},{"index":1,"embedding":[2]}]}`,
			`{"data":[{"index":2,"embedding":[1]},{"index":1,"embedding":[2]}]}`,
			`{"data":[{"index":0,"embedding":[]},{"index":1,"embedding":[2]}]}`,
			`{"data":[{"index":0,"embedding":[1,2]},{"index":1,"embedding":[2]}]}`,
			`{"data":[{"index":0,"embedding":[null]},{"index":1,"embedding":[0]}]}`,
			`{"data":[{"index":0,"embedding":[1e999]},{"index":1,"embedding":[0]}]}`,
			`{"data":[{"index":0,"embedding":["0"]},{"index":1,"embedding":[0]}]}`,
			`{"data":[{"index":0,"embedding":[0]},{"index":1,"embedding":[0]}],"error":{"message":"private-body"}}`,
			`{"data":[{"index":0,"embedding":[0]},{"index":1,"embedding":[0]}]} {}`,
		} {
			t.Run(body, func(t *testing.T) {
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
				defer server.Close()
				driver, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings"})
				vectors, err := driver.Encode(stringPtr("model"), []string{"a", "b"}, &APIConfig{APIKey: stringPtr("fixture")}, nil)
				if err == nil || vectors != nil || strings.Contains(err.Error(), "private-body") {
					t.Fatal(vectors, err)
				}
			})
		}
	})
	t.Run("HTTP error", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(429); fmt.Fprint(w, "private-body") }))
		defer server.Close()
		driver, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings"})
		vectors, err := driver.Encode(stringPtr("model"), []string{"a"}, &APIConfig{APIKey: stringPtr("fixture")}, nil)
		if err == nil || vectors != nil || strings.Contains(err.Error(), "private-body") || !strings.Contains(err.Error(), "429") {
			t.Fatal(vectors, err)
		}
	})
	t.Run("cross batch dimensions", func(t *testing.T) {
		var calls atomic.Int32
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			call := calls.Add(1)
			var body struct{ Input []string }
			json.NewDecoder(r.Body).Decode(&body)
			data := make([]map[string]any, len(body.Input))
			for i := range data {
				vector := []float64{1, 2}
				if call > 1 {
					vector = []float64{1}
				}
				data[i] = map[string]any{"index": i, "embedding": vector}
			}
			json.NewEncoder(w).Encode(map[string]any{"data": data})
		}))
		defer server.Close()
		driver, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings"})
		if vectors, err := driver.Encode(stringPtr("model"), make([]string, 33), &APIConfig{APIKey: stringPtr("fixture")}, nil); err == nil || vectors != nil || calls.Load() != 2 {
			t.Fatal(vectors, err, calls.Load())
		}
	})
	t.Run("preflight and cancellation", func(t *testing.T) {
		started := make(chan struct{})
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			io.Copy(io.Discard, r.Body)
			r.Body.Close()
			close(started)
			<-r.Context().Done()
		}))
		defer server.Close()
		driver, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings"})
		for _, api := range []*APIConfig{nil, {}, {APIKey: stringPtr(" ")}, {APIKey: stringPtr("fixture"), Region: stringPtr("missing")}} {
			if vectors, err := driver.Encode(stringPtr("model"), []string{"a"}, api, nil); err == nil || vectors != nil {
				t.Fatal(vectors, err)
			}
		}
		if vectors, err := driver.Encode(nil, []string{"a"}, &APIConfig{APIKey: stringPtr("fixture")}, nil); err == nil || vectors != nil {
			t.Fatal(vectors, err)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		result := make(chan error, 1)
		go func() {
			_, err := driver.Encode(stringPtr("model"), []string{"a"}, &APIConfig{APIKey: stringPtr("fixture"), Context: ctx}, nil)
			result <- err
		}()
		select {
		case <-started:
			cancel()
		case <-ctx.Done():
			t.Fatal("embedding did not reach HTTP boundary")
		}
		select {
		case err := <-result:
			if !errors.Is(err, context.Canceled) {
				t.Fatal(err)
			}
		case <-time.After(3 * time.Second):
			t.Fatal("embedding ignored cancellation")
		}
	})
}
