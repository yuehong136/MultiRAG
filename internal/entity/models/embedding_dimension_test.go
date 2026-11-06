package models

import (
	"encoding/json"
	"fmt"
	"net/http"
	"reflect"
	"testing"
)

func TestBoundEmbeddingCatalogDimensions(t *testing.T) {
	maxDimension := 4
	for _, tc := range []struct {
		name      string
		requested int
		maximum   *int
		options   []int
		valid     bool
	}{
		{"default", 0, &maxDimension, []int{2, 4}, true},
		{"listed", 2, &maxDimension, []int{2, 4}, true},
		{"unsupported option", 3, &maxDimension, []int{2, 4}, false},
		{"within maximum", 3, &maxDimension, nil, true},
		{"above maximum", 5, &maxDimension, nil, false},
		{"unknown", 5, nil, nil, true},
		{"negative", -1, nil, nil, false},
		{"empty options", 3, &maxDimension, []int{}, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			calls := 0
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				var body struct {
					Dimensions int `json:"dimensions"`
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if body.Dimensions != tc.requested {
					t.Errorf("dimension=%d", body.Dimensions)
				}
				dimension := tc.requested
				if dimension == 0 {
					dimension = 4
				}
				data := []map[string]any{{"index": 0, "embedding": make([]float64, dimension)}}
				json.NewEncoder(w).Encode(map[string]any{"data": data})
			}))
			driver := NewOpenAIModel(map[string]string{"default": srv.URL}, URLSuffix{Embedding: "embeddings"})
			bound := NewEmbeddingModel(driver, stringPtr("callable/alias"), &APIConfig{APIKey: stringPtr("key")})
			bound.MaxDimension, bound.Dimensions = tc.maximum, tc.options
			bound.EmbeddingConfig = &EmbeddingConfig{Dimension: tc.requested}
			result, err := bound.Embed([]string{"query"})
			if (err == nil) != tc.valid || !tc.valid && (result != nil || calls != 0) || tc.valid && calls != 1 {
				t.Fatalf("result=%v calls=%d err=%v", result, calls, err)
			}
		})
	}
}

func TestVolcEngineRequestedDimensionWire(t *testing.T) {
	for _, dimension := range []int{0, 2} {
		t.Run(fmt.Sprint(dimension), func(t *testing.T) {
			calls := 0
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if body["model"] != "owner/real-id" {
					t.Error(body)
				}
				value, present := body["dimensions"]
				if dimension == 0 && present || dimension > 0 && value != float64(dimension) {
					t.Error(body)
				}
				fmt.Fprint(w, `{"data":{"embedding":[1,2]}}`)
			}))
			driver := NewVolcEngine(map[string]string{"default": srv.URL}, URLSuffix{Embedding: "embed"})
			result, err := driver.Embed(stringPtr("owner/real-id"), []string{"a", "b"}, &APIConfig{APIKey: stringPtr("key")}, &EmbeddingConfig{Dimension: dimension})
			want := []EmbeddingData{{Index: 0, Embedding: []float64{1, 2}}, {Index: 1, Embedding: []float64{1, 2}}}
			if err != nil || calls != 2 || !reflect.DeepEqual(result, want) {
				t.Fatal(result, calls, err)
			}
		})
	}
}

func TestReplicateDiscoveryCallableIDs(t *testing.T) {
	for _, tc := range []struct {
		raw   string
		names []string
	}{
		{`{"results":[{"id":"owner/first","owner":"other","name":"ignored"},{"owner":"owner","name":"second"}]}`, []string{"owner/first", "owner/second"}},
		{`{"results":[]}`, []string{}},
		{`{"results":[{"id":"../bad"}]}`, nil},
		{`{"results":[{"id":"owner/a"},{"owner":"owner","name":"a"}]}`, nil},
		{`{"results":[{"id":" owner/a"}]}`, nil},
		{`{"results":[{"id":"owner/a"}]} {}`, nil},
		{`{"results":null}`, nil},
	} {
		t.Run(tc.raw, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, tc.raw) }))
			names, err := newReplicateTestModel(srv.URL).ListModels(&APIConfig{APIKey: stringPtr("key")})
			if (err == nil) != (tc.names != nil) || !reflect.DeepEqual(names, tc.names) {
				t.Fatal(names, err)
			}
		})
	}
}
