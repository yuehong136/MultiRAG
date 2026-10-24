package models

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
)

func TestPerplexityNativeInt8BatchesAndDimensions(t *testing.T) {
	for _, test := range []struct {
		name                 string
		dimension, requested int
	}{{"pplx-embed-v1-0.6b", 128, 128}, {"pplx-embed-v1-0.6b", 1024, 0}, {"pplx-embed-v1-4b", 2560, 0}} {
		t.Run(fmt.Sprint(test), func(t *testing.T) {
			calls := 0
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body struct {
					Input      []string
					Model      string
					Dimensions int
					Encoding   string `json:"encoding_format"`
					Query      any
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if r.URL.Path != "/v1/embeddings" || r.Header.Get("Authorization") != "Bearer key" || body.Encoding != "base64_int8" || body.Dimensions != test.dimension || body.Model != test.name || body.Query != nil {
					t.Error(body, r.URL)
				}
				calls++
				data := []any{}
				for i := len(body.Input) - 1; i >= 0; i-- {
					index, _ := strconv.Atoi(body.Input[i])
					raw := make([]byte, test.dimension)
					raw[0] = byte(index)
					raw[1] = 255
					raw[2] = 128
					raw[3] = 127
					data = append(data, map[string]any{"index": i, "embedding": base64.StdEncoding.EncodeToString(raw)})
				}
				json.NewEncoder(w).Encode(map[string]any{"data": data})
			}))
			defer srv.Close()
			texts := make([]string, 33)
			for i := range texts {
				texts[i] = strconv.Itoa(i)
			}
			d := NewPerplexityModel(map[string]string{"default": srv.URL + "/v1/"}, URLSuffix{Embedding: "v1/embeddings"})
			result, err := d.Embed(&test.name, texts, &APIConfig{APIKey: stringPtr("key")}, &EmbeddingConfig{Dimension: test.requested, Query: true})
			if err != nil || len(result) != 33 || calls != 2 {
				t.Fatal(result, err, calls)
			}
			for i, item := range result {
				if item.Index != i || len(item.Embedding) != test.dimension || item.Embedding[0] != float64(i) || item.Embedding[1] != -1 || item.Embedding[2] != -128 || item.Embedding[3] != 127 || item.Embedding[4] != 0 {
					t.Fatal(i, item)
				}
			}
		})
	}
}

func TestPerplexityRejectsInvalidEncodingShapeAndIndices(t *testing.T) {
	good := base64.StdEncoding.EncodeToString(make([]byte, 128))
	for _, body := range []string{
		`null`, `{}`, `{"data":null}`, `{"data":[]}`, `{"data":[{"index":0,"embedding":[0,1]}]}`, `{"data":[{"index":0,"embedding":null}]}`,
		`{"data":[{"index":0,"embedding":"` + good[:len(good)-4] + `"}]}`, `{"data":[{"index":0,"embedding":"!` + good[1:] + `"}]}`,
		`{"data":[{"embedding":"` + good + `"}]}`, `{"data":[{"index":null,"embedding":"` + good + `"}]}`, `{"data":[{"index":1,"embedding":"` + good + `"}]}`,
		`{"data":[{"index":0,"embedding":"` + good + `"}],"error":"private-secret"}`,
	} {
		t.Run(body[:min(len(body), 70)], func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
			defer srv.Close()
			d := NewPerplexityModel(map[string]string{"default": srv.URL}, URLSuffix{Embedding: "embeddings"})
			name := "pplx-embed-v1-0.6b"
			data, err := d.Embed(&name, []string{"q"}, &APIConfig{APIKey: stringPtr("key")}, &EmbeddingConfig{Dimension: 128})
			if err == nil || data != nil || strings.Contains(err.Error(), "private-secret") {
				t.Fatal(data, err)
			}
		})
	}
}

func TestPerplexityEmbeddingPreflightAndNoPartialBatch(t *testing.T) {
	d := NewPerplexityModel(map[string]string{"default": "http://127.0.0.1:1"}, URLSuffix{Embedding: "embeddings"})
	name := "pplx-embed-v1-0.6b"
	if data, err := d.Embed(nil, nil, nil, nil); err != nil || data == nil {
		t.Fatal(data, err)
	}
	for _, dimension := range []int{-1, 16, 127, 1025, 2561} {
		if data, err := d.Embed(&name, []string{"q"}, nil, &EmbeddingConfig{Dimension: dimension}); err == nil || data != nil {
			t.Fatal(data, err)
		}
	}
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 2 {
			fmt.Fprint(w, `{"data":[]}`)
			return
		}
		data := []any{}
		for i := range 32 {
			data = append(data, map[string]any{"index": i, "embedding": base64.StdEncoding.EncodeToString(make([]byte, 128))})
		}
		json.NewEncoder(w).Encode(map[string]any{"data": data})
	}))
	defer srv.Close()
	d = NewPerplexityModel(map[string]string{"default": srv.URL}, URLSuffix{Embedding: "embeddings"})
	texts := make([]string, 33)
	for i := range texts {
		texts[i] = "q"
	}
	if data, err := d.Embed(&name, texts, &APIConfig{APIKey: stringPtr("key")}, &EmbeddingConfig{Dimension: 128}); err == nil || data != nil || calls != 2 {
		t.Fatal(data, err, calls)
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if _, err := d.Embed(&name, []string{"q"}, &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}
