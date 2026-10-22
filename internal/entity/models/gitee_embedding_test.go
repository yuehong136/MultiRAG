package models

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
)

func TestGiteeEmbeddingDimensionAndRegionHTTP(t *testing.T) {
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Path != "/v1/embeddings" || r.Header.Get("Authorization") != "Bearer key" {
			t.Error(r.URL.Path, "credential or endpoint missing")
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
		if body.Model != "BAAI/bge-m3" || body.Dimensions != 2 || body.Query != nil {
			t.Error(body)
		}
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
	name, key, unknown := "BAAI/bge-m3", "key", "unknown"
	driver := NewGiteeModel(map[string]string{"default": server.URL + "/v1/"}, URLSuffix{Embedding: "embeddings"})
	texts := make([]string, 33)
	for i := range texts {
		texts[i] = strconv.Itoa(i)
	}
	api := &APIConfig{APIKey: &key, Region: &unknown}
	cfg := &EmbeddingConfig{Dimension: 2, Query: true}
	vectors, err := driver.Encode(&name, texts, api, cfg)
	if err != nil || len(vectors) != 33 || calls != 2 {
		t.Fatal(len(vectors), calls, err)
	}
	for i, v := range vectors {
		if len(v) != 2 || v[0] != 0 || v[1] != float64(i) {
			t.Fatal(i, v)
		}
	}
	for _, keyless := range []*APIConfig{nil, {}, {APIKey: stringPtr(" ")}} {
		if got, err := driver.Encode(&name, []string{"0"}, keyless, cfg); err == nil || got != nil {
			t.Fatal("missing key accepted")
		}
	}
	if calls != 2 || unknown != "unknown" || !cfg.Query {
		t.Fatal("invalid request or config mutation")
	}
}
