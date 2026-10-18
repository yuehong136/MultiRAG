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

func TestAliyunEmbeddingBatchRegionAndOrderHTTP(t *testing.T) {
	var batches []int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/intl/compatible-mode/v1/embeddings" || r.Header.Get("Authorization") != "Bearer fixture" {
			t.Error("embedding endpoint/auth contract")
		}
		var body struct {
			Model string
			Input []string
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Model != "text-embedding-v4" {
			t.Error(body, err)
		}
		batches = append(batches, len(body.Input))
		data := make([]map[string]interface{}, 0, len(body.Input))
		for i := len(body.Input) - 1; i >= 0; i-- {
			var n int
			if _, err := fmt.Sscanf(body.Input[i], "text-%d", &n); err != nil {
				t.Error(err)
			}
			data = append(data, map[string]interface{}{"index": i, "embedding": []float64{float64(n), 9}})
		}
		json.NewEncoder(w).Encode(map[string]interface{}{"data": data})
	}))
	defer server.Close()
	driver := NewAliyunModel(map[string]string{"default": "http://unused.invalid", "singapore": server.URL + "/intl/"}, URLSuffix{Embedding: "/compatible-mode/v1/embeddings"})
	key, name, region := "fixture", "text-embedding-v4", "singapore"
	api := &APIConfig{APIKey: &key, Region: &region}
	bound := NewEmbeddingModel(driver, &name, api)
	texts := make([]string, 33)
	want := make([][]float64, 33)
	for i := range texts {
		texts[i] = fmt.Sprintf("text-%d", i)
		want[i] = []float64{float64(i), 9}
	}
	got, err := bound.Encode(texts)
	if err != nil || !reflect.DeepEqual(got, want) || !reflect.DeepEqual(batches, []int{32, 1}) || api.Context != nil {
		t.Fatal(got, batches, err)
	}
}

func TestAliyunEmbeddingRejectsInvalidResponsesHTTP(t *testing.T) {
	for _, tc := range []struct {
		name, body string
		status     int
	}{
		{"missing index", `{"data":[{"embedding":[1,2]},{"index":1,"embedding":[3,4]}]}`, 200},
		{"null index", `{"data":[{"index":null,"embedding":[1,2]},{"index":1,"embedding":[3,4]}]}`, 200},
		{"duplicate index", `{"data":[{"index":0,"embedding":[1,2]},{"index":0,"embedding":[3,4]}]}`, 200},
		{"missing result", `{"data":[{"index":0,"embedding":[1,2]}]}`, 200},
		{"extra result", `{"data":[{"index":0,"embedding":[1]},{"index":1,"embedding":[2]},{"index":2,"embedding":[3]}]}`, 200},
		{"out of range", `{"data":[{"index":0,"embedding":[1,2]},{"index":2,"embedding":[3,4]}]}`, 200},
		{"negative index", `{"data":[{"index":-1,"embedding":[1,2]},{"index":1,"embedding":[3,4]}]}`, 200},
		{"fractional index", `{"data":[{"index":0.5,"embedding":[1,2]},{"index":1,"embedding":[3,4]}]}`, 200},
		{"empty vector", `{"data":[{"index":0,"embedding":[]},{"index":1,"embedding":[3,4]}]}`, 200},
		{"wrong dimension", `{"data":[{"index":0,"embedding":[1]},{"index":1,"embedding":[3,4]}]}`, 200},
		{"nonfinite", `{"data":[{"index":0,"embedding":[1e999]},{"index":1,"embedding":[3]}]}`, 200},
		{"wrong vector type", `{"data":[{"index":0,"embedding":["private-body"]},{"index":1,"embedding":[3]}]}`, 200},
		{"business error", `{"error":{"message":"private-body"}}`, 200},
		{"HTTP failure", `private-body`, 429}, {"malformed", `{`, 200}, {"null", `null`, 200},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(tc.status); fmt.Fprint(w, tc.body) }))
			defer server.Close()
			driver := NewAliyunModel(map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings"})
			key, name := "fixture", "text-embedding-v4"
			got, err := driver.Encode(&name, []string{"first", "second"}, &APIConfig{APIKey: &key}, nil)
			if err == nil || got != nil || strings.Contains(err.Error(), "private-body") {
				t.Fatal(got, err)
			}
		})
	}
}

func TestAliyunEmbeddingPreflightAndCancel(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		fmt.Fprint(w, `{"data":[{"index":0,"embedding":[1]}]}`)
	}))
	defer server.Close()
	driver := NewAliyunModel(map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings"})
	key, name, unknown := "fixture", "text-embedding-v4", "missing"
	if got, err := driver.Encode(nil, nil, nil, nil); err != nil || got == nil || len(got) != 0 {
		t.Fatal(got, err)
	}
	for _, tc := range []struct {
		name *string
		api  *APIConfig
	}{{nil, &APIConfig{APIKey: &key}}, {&name, nil}, {&name, &APIConfig{}}, {&name, &APIConfig{APIKey: &key, Region: &unknown}}} {
		if got, err := driver.Encode(tc.name, []string{"a"}, tc.api, nil); err == nil || got != nil {
			t.Fatal(got, err)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if got, err := driver.Encode(&name, []string{"a"}, &APIConfig{APIKey: &key, Context: ctx}, nil); !errors.Is(err, context.Canceled) || got != nil {
		t.Fatal(got, err)
	}
	if calls.Load() != 0 {
		t.Fatal("empty/invalid/canceled embedding request reached HTTP")
	}
}

func TestAliyunEmbeddingCancelDuringHTTP(t *testing.T) {
	started, stopped := make(chan struct{}), make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		r.Body.Close()
		w.WriteHeader(http.StatusOK)
		w.(http.Flusher).Flush()
		close(started)
		select {
		case <-r.Context().Done():
		case <-time.After(2 * time.Second):
			t.Error("embedding request did not cancel")
		}
		close(stopped)
	}))
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go func() { <-started; cancel() }()
	key, name := "fixture", "text-embedding-v4"
	driver := NewAliyunModel(map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings"})
	got, err := driver.Encode(&name, []string{"one"}, &APIConfig{APIKey: &key, Context: ctx}, nil)
	if !errors.Is(err, context.Canceled) || got != nil {
		t.Fatal(got, err)
	}
	select {
	case <-stopped:
	case <-time.After(time.Second):
		t.Fatal("embedding HTTP cleanup failed")
	}
}
