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

func TestNVIDIAEmbeddingParametersAndRegionsHTTP(t *testing.T) {
	for _, config := range []*EmbeddingConfig{nil, {}, {Query: true, Dimension: 2}} {
		for _, region := range []*string{nil, stringPtr(""), stringPtr("office"), stringPtr("unknown")} {
			t.Run(fmt.Sprint(config, region), func(t *testing.T) {
				var calls atomic.Int32
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					calls.Add(1)
					var body map[string]any
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
					}
					inputType := "query"
					if config != nil && !config.Query {
						inputType = "passage"
					}
					path := "/default/v1/embeddings"
					if region != nil && *region == "office" {
						path = "/office/v1/embeddings"
					}
					if r.Method != http.MethodPost || r.URL.Path != path || r.Header.Get("Authorization") != "Bearer fixture" || r.Header.Get("Content-Type") != "application/json" || body["model"] != "embed" || body["input_type"] != inputType || body["truncate"] != "END" || body["encoding_format"] != "float" {
						t.Error("NVIDIA request configuration", body, r.URL.Path)
					}
					if _, exists := body["query"]; exists {
						t.Error("local Query field leaked")
					}
					if config != nil && config.Dimension > 0 {
						if body["dimensions"] != float64(2) {
							t.Error("requested dimension lost")
						}
					} else if _, exists := body["dimensions"]; exists {
						t.Error("default dimensions emitted")
					}
					fmt.Fprint(w, `{"data":[{"index":1,"embedding":[1,0]},{"index":0,"embedding":[0,1]}]}`)
				}))
				defer server.Close()
				urls := map[string]string{"default": server.URL + "/default/v1/", "office": server.URL + "/office/v1/"}
				driver := NewNVIDIAModel(urls, URLSuffix{Embedding: "embeddings"})
				urls["default"], urls["office"] = "http://mutated.invalid", "http://mutated.invalid"
				api := &APIConfig{APIKey: stringPtr(" fixture "), Region: region}
				vectors, err := embeddingVectorsForTest(driver.Embed(stringPtr("embed"), []string{"first", "second"}, api, config))
				if err != nil || !reflect.DeepEqual(vectors, [][]float64{{0, 1}, {1, 0}}) || calls.Load() != 1 || api.Context != nil || api.Region != region || *api.APIKey != " fixture " {
					t.Fatal(vectors, err, calls.Load())
				}
			})
		}
	}
}

func TestNVIDIAEmbeddingRejectsMalformedHTTP(t *testing.T) {
	for _, body := range []string{
		``, `null`, `{}`, `{"data":[]}`,
		`{"data":[{"index":0,"embedding":[1]}]}`,
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
			driver := NewNVIDIAModel(map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings"})
			vectors, err := embeddingVectorsForTest(driver.Embed(stringPtr("model"), []string{"a", "b"}, &APIConfig{APIKey: stringPtr("fixture")}, nil))
			if err == nil || vectors != nil || strings.Contains(err.Error(), "private-body") {
				t.Fatal(vectors, err)
			}
		})
	}
}

func TestNVIDIAEmbeddingPreflightAndDeadline(t *testing.T) {
	driver := NewNVIDIAModel(map[string]string{"default": "http://fixture.invalid"}, URLSuffix{Embedding: "embeddings"})
	var calls int
	driver.httpClient.Transport = lmStudioTransportFunc(func(r *http.Request) (*http.Response, error) {
		calls++
		deadline, ok := r.Context().Deadline()
		if !ok || time.Until(deadline) <= 0 || time.Until(deadline) > 30*time.Second {
			t.Error("embedding request lacks its bounded 30s deadline")
		}
		return &http.Response{StatusCode: http.StatusTooManyRequests, Body: io.NopCloser(strings.NewReader("private-body")), Header: make(http.Header)}, nil
	})
	if vectors, err := embeddingVectorsForTest(driver.Embed(nil, nil, nil, &EmbeddingConfig{Dimension: -1})); err != nil || vectors == nil || len(vectors) != 0 || calls != 0 {
		t.Fatal(vectors, err, calls)
	}
	api := &APIConfig{APIKey: stringPtr("fixture")}
	for _, name := range []*string{nil, stringPtr(""), stringPtr(" ")} {
		if vectors, err := embeddingVectorsForTest(driver.Embed(name, []string{"a"}, api, nil)); err == nil || vectors != nil {
			t.Fatal("invalid name succeeded", vectors, err)
		}
	}
	for _, invalid := range []*APIConfig{nil, {}, {APIKey: stringPtr("")}, {APIKey: stringPtr(" ")}} {
		if vectors, err := embeddingVectorsForTest(driver.Embed(stringPtr("embed"), []string{"a"}, invalid, nil)); err == nil || vectors != nil {
			t.Fatal("missing key succeeded", vectors, err)
		}
	}
	if vectors, err := embeddingVectorsForTest(driver.Embed(stringPtr("embed"), []string{"a"}, api, &EmbeddingConfig{Dimension: -1})); err == nil || vectors != nil || calls != 0 {
		t.Fatal(vectors, err, calls)
	}
	for _, missing := range []*NVIDIAModel{NewNVIDIAModel(nil, URLSuffix{Embedding: "embeddings"}), NewNVIDIAModel(driver.BaseURL, URLSuffix{})} {
		if vectors, err := embeddingVectorsForTest(missing.Embed(stringPtr("embed"), []string{"a"}, api, nil)); err == nil || vectors != nil {
			t.Fatal("missing endpoint succeeded", vectors, err)
		}
	}
	if vectors, err := embeddingVectorsForTest(driver.Embed(stringPtr("embed"), []string{"a"}, api, nil)); err == nil || vectors != nil || !strings.Contains(err.Error(), "429") || strings.Contains(err.Error(), "private-body") || calls != 1 || api.Context != nil {
		t.Fatal(vectors, err, calls)
	}
}

func TestNVIDIAEmbeddingCancellationHTTP(t *testing.T) {
	started := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		r.Body.Close()
		close(started)
		<-r.Context().Done()
	}))
	defer server.Close()
	driver := NewNVIDIAModel(map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings"})
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	result := make(chan error, 1)
	go func() {
		vectors, err := embeddingVectorsForTest(driver.Embed(stringPtr("embed"), []string{"a"}, &APIConfig{APIKey: stringPtr("fixture"), Context: ctx}, nil))
		if vectors != nil {
			result <- fmt.Errorf("cancelled call returned vectors")
			return
		}
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
}
