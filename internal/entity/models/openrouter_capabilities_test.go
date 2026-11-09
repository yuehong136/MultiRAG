package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"

	"reflect"
	"strings"
	"testing"
	"time"
)

func TestOpenRouterBoundCapabilitiesHTTP(t *testing.T) {
	var paths []string
	server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		if r.Header.Get("Authorization") != "Bearer key" {
			t.Error("wrong credential")
		}
		if r.URL.Path == "/credits" {
			if r.Method != http.MethodGet || r.ContentLength > 0 {
				t.Error("invalid credits GET")
			}
			fmt.Fprint(w, `{"data":{"total_credits":10.5,"total_usage":2.25}}`)
			return
		}
		if r.Method != http.MethodPost {
			t.Error("invalid inference method")
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body["model"] != "model" {
			t.Error("model missing")
		}
		switch r.URL.Path {
		case "/embeddings":
			input := body["input"].([]any)
			var data []map[string]any
			for i := len(input) - 1; i >= 0; i-- {
				data = append(data, map[string]any{"index": i, "embedding": []float64{float64(i), 2}})
			}
			json.NewEncoder(w).Encode(map[string]any{"data": data})
		case "/rerank":
			if !reflect.DeepEqual(body, map[string]any{"model": "model", "query": "q", "documents": []any{"first", "second"}, "top_n": float64(2)}) {
				t.Error("invalid rerank request", body)
			}
			fmt.Fprint(w, `{"results":[{"index":1,"relevance_score":0.8},{"index":0,"relevance_score":0}]}`)
		default:
			t.Error("unexpected endpoint", r.URL.Path)
		}
	}))
	defer server.Close()
	driver, _ := NewModelFactory().CreateModelDriver("OpenRouter", map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings", Rerank: "rerank", Balance: "credits"})
	name := "model"
	api := &APIConfig{ApiKey: stringPtr("key"), Region: stringPtr("")}
	vectors, err := embeddingVectorsForTest(NewEmbeddingModel(driver, &name, api, 0).ModelDriver.Embed(NewEmbeddingModel(driver, &name, api, 0).ModelName, make([]string, 33), api, nil))
	if err != nil || len(vectors) != 33 || !reflect.DeepEqual(vectors[31], []float64{31, 2}) || !reflect.DeepEqual(vectors[32], []float64{32, 2}) {
		t.Fatal(vectors, err)
	}
	scores, err := rerankScoresForTest(NewRerankModel(driver, &name, api), "q", []string{"first", "second"}, api, &RerankConfig{})
	if err != nil || !reflect.DeepEqual(scores, []float64{0, 0.8}) {
		t.Fatal(scores, err)
	}
	balance, err := driver.Balance(api)
	if err != nil || !reflect.DeepEqual(balance, map[string]any{"total_credits": 10.5, "total_usage": 2.25, "balance": 8.25, "currency": "USD"}) {
		t.Fatal(balance, err)
	}
	if err := driver.CheckConnection(api); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(paths, []string{"/embeddings", "/rerank", "/credits", "/credits"}) {
		t.Fatal(paths)
	}
}

func TestOpenRouterMalformedCapabilitiesHTTP(t *testing.T) {
	for _, test := range []struct {
		path, body string
		count      int
	}{
		{"embeddings", `{}`, 1},
		{"embeddings", `{"data":[{"embedding":[1]}]}`, 1},
		{"embeddings", `{"data":[{"index":0,"embedding":[1]},{"index":0,"embedding":[2]}]}`, 2},
		{"embeddings", `{"data":[{"index":0,"embedding":[]}]}`, 1},
		{"embeddings", `{"data":[{"index":0,"embedding":[1]},{"index":1,"embedding":[2,3]}]}`, 2},
		{"embeddings", `{"error":"private-body","data":[{"index":0,"embedding":[1]}]}`, 1},
		{"embeddings", `{"data":[{"index":0,"embedding":[1e999]}]}`, 1},
		{"rerank", `{}`, 1},
		{"rerank", `{"results":[{"relevance_score":1}]}`, 1},
		{"rerank", `{"results":[{"index":0}]}`, 1},
		{"rerank", `{"results":[{"index":0,"relevance_score":1},{"index":0,"relevance_score":0}]}`, 2},
		{"rerank", `{"results":[{"index":2,"relevance_score":1}]}`, 1},
		{"rerank", `{"results":[{"index":0.5,"relevance_score":1}]}`, 1},
		{"rerank", `{"error":"private-body","results":[{"index":0,"relevance_score":0}]}`, 1},
		{"rerank", `{"results":[{"index":0,"relevance_score":1e999}]}`, 1},
		{"credits", `{}`, 1}, {"credits", `{"data":{}}`, 1},
		{"credits", `{"data":{"total_credits":0}}`, 1},
		{"credits", `{"data":{"total_credits":null,"total_usage":0}}`, 1},
		{"credits", `{"data":{"total_credits":1e308,"total_usage":-1e308}}`, 1},
		{"credits", `{"error":"private-body","data":{"total_credits":0,"total_usage":0}}`, 1},
	} {
		t.Run(test.path+test.body, func(t *testing.T) {
			server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, test.body) }))
			defer server.Close()
			driver := NewOpenRouterModel(map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings", Rerank: "rerank", Balance: "credits"})
			api := &APIConfig{ApiKey: stringPtr("key")}
			name := "model"
			var err error
			switch test.path {
			case "embeddings":
				_, err = embeddingVectorsForTest(driver.Embed(&name, make([]string, test.count), api, nil))
			case "rerank":
				_, err = rerankScoresForTest(NewRerankModel(driver, &name, api), "q", make([]string, test.count), api, &RerankConfig{})
			case "credits":
				err = driver.CheckConnection(api)
			}
			if err == nil || strings.Contains(err.Error(), "private-body") {
				t.Fatal("malformed response accepted or leaked", err)
			}
		})
	}
}

func TestOpenRouterCapabilitiesPreflightAndHTTPStatus(t *testing.T) {
	driver := NewOpenRouterModel(map[string]string{"default": "http://invalid.invalid"}, URLSuffix{})
	if vectors, err := embeddingVectorsForTest(driver.Embed(nil, nil, &APIConfig{ApiKey: stringPtr("key")}, nil)); err != nil || vectors == nil || len(vectors) != 0 {
		t.Fatal(vectors, err)
	}
	if scores, err := rerankScoresForTest(NewRerankModel(driver, nil, &APIConfig{ApiKey: stringPtr("key")}), "", nil, &APIConfig{ApiKey: stringPtr("key")}, &RerankConfig{}); err != nil || len(scores) != 0 {
		t.Fatal(scores, err)
	}
	for _, path := range []string{"embeddings", "rerank", "credits"} {
		t.Run(path, func(t *testing.T) {
			server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(http.StatusUnauthorized)
				fmt.Fprint(w, "private-body")
			}))
			defer server.Close()
			driver := NewOpenRouterModel(map[string]string{"default": server.URL}, URLSuffix{Embedding: path, Rerank: path, Balance: path})
			name := "model"
			for _, api := range []*APIConfig{nil, {}, {ApiKey: stringPtr("key")}} {
				var err error
				switch path {
				case "embeddings":
					_, err = embeddingVectorsForTest(driver.Embed(&name, []string{"x"}, api, nil))
				case "rerank":
					_, err = rerankScoresForTest(NewRerankModel(driver, &name, api), "q", []string{"x"}, api, &RerankConfig{})
				case "credits":
					err = driver.CheckConnection(api)
				}
				if err == nil || strings.Contains(err.Error(), "private-body") {
					t.Fatal(err)
				}
			}
		})
	}
}

func TestOpenRouterCapabilitiesCancellationHTTP(t *testing.T) {
	for _, path := range []string{"embeddings", "rerank", "credits"} {
		t.Run(path, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			stopped := make(chan struct{})
			server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				defer close(stopped)
				if r.Body != nil {
					var body any
					_ = json.NewDecoder(r.Body).Decode(&body)
					r.Body.Close()
				}
				w.WriteHeader(200)
				w.(http.Flusher).Flush()
				cancel()
				<-r.Context().Done()
			}))
			defer server.Close()
			driver := NewOpenRouterModel(map[string]string{"default": server.URL}, URLSuffix{Embedding: path, Rerank: path, Balance: path})
			api := &APIConfig{ApiKey: stringPtr("key"), Context: ctx}
			name := "model"
			var err error
			switch path {
			case "embeddings":
				_, err = embeddingVectorsForTest(driver.Embed(&name, []string{"x"}, api, nil))
			case "rerank":
				_, err = rerankScoresForTest(NewRerankModel(driver, &name, api), "q", []string{"x"}, api, &RerankConfig{})
			case "credits":
				err = driver.CheckConnection(api)
			}
			if !errors.Is(err, context.Canceled) {
				t.Fatal(err)
			}
			select {
			case <-stopped:
			case <-time.After(time.Second):
				t.Fatal("response not released")
			}
		})
	}
}
