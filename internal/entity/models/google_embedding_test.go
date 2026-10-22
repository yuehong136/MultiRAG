package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestGoogleEmbeddingSDKBatchAndTaskConfig(t *testing.T) {
	for _, config := range []*EmbeddingConfig{nil, {}, {Dimension: 3}, {Query: true, Dimension: 3}} {
		t.Run(fmt.Sprintf("%+v", config), func(t *testing.T) {
			var calls atomic.Int32
			texts := []string{"first", "中文", "  keep spacing  "}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.Method != http.MethodPost || r.URL.Path != "/proxy/v1beta/models/text-embedding-004:batchEmbedContents" || r.Header.Get("x-goog-api-key") != "fixture-key" {
					t.Error("embedding endpoint, method or authentication lost")
				}
				var payload struct {
					Requests []struct {
						Model   string `json:"model"`
						Content struct {
							Role  string                  `json:"role"`
							Parts []struct{ Text string } `json:"parts"`
						} `json:"content"`
						TaskType  string `json:"taskType"`
						Dimension *int32 `json:"outputDimensionality"`
					} `json:"requests"`
				}
				if err := json.NewDecoder(r.Body).Decode(&payload); err != nil || len(payload.Requests) != len(texts) {
					t.Errorf("batch request: %+v, %v", payload, err)
				}
				for i, request := range payload.Requests {
					if request.Model != "models/text-embedding-004" || request.Content.Role != "user" || len(request.Content.Parts) != 1 || request.Content.Parts[0].Text != texts[i] {
						t.Errorf("input %d changed: %+v", i, request)
					}
					wantTask := "RETRIEVAL_DOCUMENT"
					if config != nil && config.Query {
						wantTask = "RETRIEVAL_QUERY"
					}
					if request.TaskType != wantTask {
						t.Errorf("task %s, want %s", request.TaskType, wantTask)
					}
					if config != nil && config.Dimension > 0 {
						if request.Dimension == nil || int(*request.Dimension) != config.Dimension {
							t.Error("requested dimension lost")
						}
					} else if request.Dimension != nil {
						t.Error("default dimension should be absent")
					}
				}
				fmt.Fprint(w, `{"embeddings":[{"values":[0,1,2]},{"values":[3,4,5]},{"values":[6,7,8]}]}`)
			}))
			defer server.Close()
			driver := NewGoogleModel(map[string]string{"default": "http://127.0.0.1:1", "fixture": server.URL + "/proxy"}, URLSuffix{})
			name, region, key := "text-embedding-004", "fixture", " fixture-key "
			api := &APIConfig{Region: &region, APIKey: &key}
			bound := NewEmbeddingModel(driver, &name, api)
			bound.EmbeddingConfig = config
			vectors, err := embeddingVectorsForTest(bound.Embed(texts))
			if err != nil || !reflect.DeepEqual(vectors, [][]float64{{0, 1, 2}, {3, 4, 5}, {6, 7, 8}}) || calls.Load() != 1 {
				t.Fatalf("vectors %v, calls %d, err %v", vectors, calls.Load(), err)
			}
			if api.Context != nil || key != " fixture-key " || (config != nil && (config.Dimension != 0 && config.Dimension != 3)) {
				t.Fatal("request configuration mutated")
			}
		})
	}
}

func TestGoogleEmbeddingRejectsMalformedSDKResponses(t *testing.T) {
	for _, body := range []string{
		`{}`, `null`, `{"embeddings":[]}`, `{"embeddings":null}`,
		`{"embeddings":[{"values":[1,2]}]}`,
		`{"embeddings":[{"values":[1,2]},{"values":[3,4]},{"values":[5,6]}]}`,
		`{"embeddings":[null,{"values":[3,4]}]}`,
		`{"embeddings":[{},{"values":[3,4]}]}`,
		`{"embeddings":[{"values":[]},{"values":[3,4]}]}`,
		`{"embeddings":[{"values":[1,null]},{"values":[3,4]}]}`,
		`{"embeddings":[{"values":[0,0]},{"values":[3,4]}]}`,
		`{"embeddings":[{"values":[1,2]},{"values":[3]}]}`,
		`{"embeddings":[{"values":[1,"NaN"]},{"values":[3,4]}]}`,
		`{"embeddings":[{"values":[1,1e39]},{"values":[3,4]}]}`,
		`{"embeddings":[{"values":[1,2]},{"values":[3,4]}],"error":{"message":"fixture-key private body"}}`,
		`{"embeddings":[{"values":[1,2]},{"values":[3,4]}]} {}`,
	} {
		t.Run(body, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
			defer server.Close()
			driver := NewGoogleModel(map[string]string{"default": server.URL}, URLSuffix{})
			vectors, err := embeddingVectorsForTest(driver.Embed(stringPtr("text-embedding-004"), []string{"a", "b"}, &APIConfig{APIKey: stringPtr("fixture-key")}, nil))
			if err == nil || vectors != nil || strings.Contains(err.Error(), "fixture-key") || strings.Contains(err.Error(), "private body") {
				t.Fatalf("invalid response accepted or unsafe error: %v %v", vectors, err)
			}
		})
	}
	for _, dimension := range []int{1, 3} {
		t.Run(fmt.Sprint(dimension), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, `{"embeddings":[{"values":[1,2]}]}`) }))
			defer server.Close()
			driver := NewGoogleModel(map[string]string{"default": server.URL}, URLSuffix{})
			if vectors, err := embeddingVectorsForTest(driver.Embed(stringPtr("text-embedding-004"), []string{"a"}, &APIConfig{APIKey: stringPtr("fixture-key")}, &EmbeddingConfig{Dimension: dimension})); err == nil || vectors != nil {
				t.Fatalf("dimension mismatch: %v %v", vectors, err)
			}
		})
	}
}

func TestGoogleEmbeddingPreflightAndCancellation(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		fmt.Fprint(w, `{"embeddings":[{"values":[1]}]}`)
	}))
	defer server.Close()
	driver := NewGoogleModel(map[string]string{"default": server.URL}, URLSuffix{})
	if vectors, err := embeddingVectorsForTest(driver.Embed(nil, nil, nil, nil)); err != nil || vectors == nil || len(vectors) != 0 || calls.Load() != 0 {
		t.Fatalf("empty batch: %v %v", vectors, err)
	}
	for _, tc := range []struct {
		name   *string
		texts  []string
		api    *APIConfig
		config *EmbeddingConfig
	}{
		{nil, []string{"a"}, &APIConfig{APIKey: stringPtr("key")}, nil},
		{stringPtr("  "), []string{"a"}, &APIConfig{APIKey: stringPtr("key")}, nil},
		{stringPtr("model"), []string{"a"}, nil, nil},
		{stringPtr("model"), []string{"a"}, &APIConfig{}, nil},
		{stringPtr("model"), []string{"a"}, &APIConfig{APIKey: stringPtr("")}, nil},
		{stringPtr("model"), []string{"a"}, &APIConfig{APIKey: stringPtr(" \t ")}, nil},
		{stringPtr("model"), []string{"a", ""}, &APIConfig{APIKey: stringPtr("key")}, nil},
		{stringPtr("model"), []string{"\n\t "}, &APIConfig{APIKey: stringPtr("key")}, nil},
		{stringPtr("model"), []string{"a"}, &APIConfig{APIKey: stringPtr("key")}, &EmbeddingConfig{Dimension: -1}},
		{stringPtr("model"), []string{"a"}, &APIConfig{APIKey: stringPtr("key")}, &EmbeddingConfig{Dimension: math.MaxInt32 + 1}},
	} {
		if vectors, err := embeddingVectorsForTest(driver.Embed(tc.name, tc.texts, tc.api, tc.config)); err == nil || vectors != nil {
			t.Fatalf("invalid input accepted: %v %v", vectors, err)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if vectors, err := embeddingVectorsForTest(driver.Embed(stringPtr("model"), []string{"a"}, &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil)); !errors.Is(err, context.Canceled) || vectors != nil {
		t.Fatalf("cancel: %v %v", vectors, err)
	}
	if calls.Load() != 0 {
		t.Fatalf("invalid inputs made %d requests", calls.Load())
	}
}

func TestGoogleEmbeddingErrorAndInFlightCancellation(t *testing.T) {
	t.Run("provider error", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusForbidden)
			fmt.Fprint(w, `{"error":{"code":403,"message":"fixture-key internal detail"}}`)
		}))
		defer server.Close()
		driver := NewGoogleModel(map[string]string{"default": server.URL}, URLSuffix{})
		vectors, err := embeddingVectorsForTest(driver.Embed(stringPtr("model"), []string{"a"}, &APIConfig{APIKey: stringPtr("fixture-key")}, nil))
		if err == nil || vectors != nil || !strings.Contains(err.Error(), "403") || strings.Contains(err.Error(), "fixture-key") || strings.Contains(err.Error(), "internal detail") {
			t.Fatalf("provider error: %v %v", vectors, err)
		}
	})
	for _, deadline := range []bool{false, true} {
		t.Run(fmt.Sprint("deadline=", deadline), func(t *testing.T) {
			started, cancelled := make(chan struct{}), make(chan struct{})
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				// Drain the SDK payload before waiting so cancellation closes the request.
				var body any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				w.WriteHeader(http.StatusOK)
				w.(http.Flusher).Flush()
				close(started)
				<-r.Context().Done()
				close(cancelled)
			}))
			defer server.Close()
			ctx, cancel := context.WithCancel(context.Background())
			if deadline {
				cancel()
				ctx, cancel = context.WithTimeout(context.Background(), 200*time.Millisecond)
			}
			defer cancel()
			driver := NewGoogleModel(map[string]string{"default": server.URL}, URLSuffix{})
			done := make(chan error, 1)
			go func() {
				vectors, err := embeddingVectorsForTest(driver.Embed(stringPtr("model"), []string{"a"}, &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil))
				if vectors != nil {
					t.Error("cancel returned vectors")
				}
				done <- err
			}()
			select {
			case <-started:
			case <-time.After(5 * time.Second):
				t.Fatal("request did not start")
			}
			if !deadline {
				cancel()
			}
			want := context.Canceled
			if deadline {
				want = context.DeadlineExceeded
			}
			select {
			case err := <-done:
				if !errors.Is(err, want) {
					t.Fatalf("cancel error %v", err)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("request did not cancel")
			}
			select {
			case <-cancelled:
			case <-time.After(5 * time.Second):
				t.Fatal("provider did not observe cancellation")
			}
		})
	}
}
