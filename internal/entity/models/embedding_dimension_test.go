package models

import (
	"encoding/json"
	"fmt"
	"net/http"
	"reflect"
	"strings"
	"testing"
)

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
			result, err := driver.Embed(stringPtr("owner/real-id"), []string{"a", "b"}, &APIConfig{ApiKey: stringPtr("key")}, &EmbeddingConfig{Dimension: dimension})
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
			names, err := newReplicateTestModel(srv.URL).ListModels(&APIConfig{ApiKey: stringPtr("key")})
			if (err == nil) != (tc.names != nil) || (tc.names != nil && joinModelNames(names, "|") != strings.Join(tc.names, "|")) {
				t.Fatal(names, err)
			}
		})
	}
}
