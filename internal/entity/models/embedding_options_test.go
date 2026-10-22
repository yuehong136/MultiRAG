package models

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

func TestEmbeddingHTTPOptionsPreserveStrictDefaults(t *testing.T) {
	name, region := "model", "unknown"
	var batches []int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" || r.URL.Path != "/v1/embeddings" {
			t.Error("optional credential or endpoint", r.URL.Path)
		}
		var body struct {
			Model      string
			Input      []string
			Dimensions int
			InputType  string `json:"input_type"`
			Query      *bool
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body.Model != name || body.Dimensions != 2 || body.InputType != "query" || body.Query != nil {
			t.Error(body)
		}
		batches = append(batches, len(body.Input))
		data := make([]map[string]any, len(body.Input))
		for i := range data {
			data[i] = map[string]any{"index": i, "embedding": []float64{0, 2}}
		}
		json.NewEncoder(w).Encode(map[string]any{"data": data})
	}))
	defer server.Close()
	urls := map[string]string{"default": server.URL + "/v1/"}
	api := &APIConfig{Region: &region}
	cfg := &EmbeddingConfig{Query: true, Dimension: 2}
	for _, option := range []embeddingHTTPOptions{{}, {optionalAPIKey: true}} {
		vectors, err := encodeHTTPWithConfig(server.Client(), urls, "embeddings", &name, []string{"a"}, api, cfg, option)
		if err == nil || vectors != nil {
			t.Fatal("strict default bypassed", vectors, err)
		}
	}
	if len(batches) != 0 {
		t.Fatal("preflight reached provider")
	}
	vectors, err := encodeHTTPWithConfig(server.Client(), urls, "embeddings", &name, make([]string, 33), api, cfg, embeddingHTTPOptions{optionalAPIKey: true, fallbackRegion: true, parameters: map[string]any{"input_type": "query"}})
	if err != nil || len(vectors) != 33 || vectors[0][0] != 0 || !reflect.DeepEqual(batches, []int{32, 1}) {
		t.Fatal(vectors, batches, err)
	}
	if api.Region != &region || region != "unknown" || !cfg.Query || cfg.Dimension != 2 {
		t.Fatal("caller configuration mutated")
	}
	deadline, ok := requestContext(api).Deadline()
	if ok {
		t.Fatal(fmt.Sprint("caller context changed", deadline))
	}
}
