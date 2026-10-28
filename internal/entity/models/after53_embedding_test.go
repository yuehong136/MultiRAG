package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func after53GPUStack(t *testing.T, base string) *GPUStackModel {
	t.Helper()
	raw, err := os.ReadFile("../../../configs/models/gpustack.json")
	if err != nil {
		t.Fatal(err)
	}
	var catalog struct {
		Suffix URLSuffix `json:"url_suffix"`
	}
	if err := json.Unmarshal(raw, &catalog); err != nil {
		t.Fatal(err)
	}
	if catalog.Suffix.Embedding != "v1-openai/embeddings" {
		t.Fatal(catalog)
	}
	return NewGPUStackModel(map[string]string{"default": base}, catalog.Suffix)
}

func TestAfter53GPUStackEmbeddingBatches(t *testing.T) {
	for _, ending := range []string{"", "/v1/"} {
		t.Run(ending, func(t *testing.T) {
			var calls atomic.Int32
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				call := calls.Add(1)
				var body struct {
					Model      string   `json:"model"`
					Input      []string `json:"input"`
					Dimensions int      `json:"dimensions"`
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				if r.Method != "POST" || r.URL.Path != "/instance/v1-openai/embeddings" || r.Header.Get("Authorization") != "Bearer key" || body.Model != "served" || body.Dimensions != 2 || len(body.Input) != map[int32]int{1: 32, 2: 1}[call] {
					t.Error("catalog endpoint/auth/batch contract", r.URL, body, call)
				}
				data := []map[string]any{}
				for i := len(body.Input) - 1; i >= 0; i-- {
					data = append(data, map[string]any{"index": i, "embedding": []float64{float64(len(body.Input[i])), 0}})
				}
				json.NewEncoder(w).Encode(map[string]any{"data": data})
			}))
			driver := after53GPUStack(t, srv.URL+"/instance"+ending)
			bound := NewEmbeddingModel(driver, stringPtr("served"), &APIConfig{APIKey: stringPtr("key")})
			bound.EmbeddingConfig = &EmbeddingConfig{Dimension: 2}
			texts := make([]string, 33)
			for i := range texts {
				texts[i] = strings.Repeat("a", i+1)
			}
			data, err := bound.Embed(texts)
			if err != nil || len(data) != 33 || calls.Load() != 2 {
				t.Fatal(data, err, calls.Load())
			}
			for i, v := range data {
				if v.Index != i || len(v.Embedding) != 2 || v.Embedding[0] != float64(i+1) || v.Embedding[1] != 0 {
					t.Fatal(i, v)
				}
			}
		})
	}
}

func TestAfter53EmbeddingNoHTTPPreflight(t *testing.T) {
	for _, provider := range []string{"GPUStack", "HunYuan"} {
		t.Run(provider, func(t *testing.T) {
			var calls atomic.Int32
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); w.WriteHeader(500) }))
			var d ModelDriver = after53GPUStack(t, srv.URL)
			if provider == "HunYuan" {
				d = NewHunyuanModel(map[string]string{"default": srv.URL}, URLSuffix{Embedding: "embeddings"})
			}
			if v, err := d.Embed(nil, nil, nil, nil); err != nil || v == nil || len(v) != 0 {
				t.Fatal(v, err)
			}
			cases := []struct {
				name *string
				api  *APIConfig
				cfg  *EmbeddingConfig
			}{
				{nil, &APIConfig{APIKey: stringPtr("key")}, nil},
				{stringPtr(""), &APIConfig{APIKey: stringPtr("key")}, nil},
				{stringPtr(" \t"), &APIConfig{APIKey: stringPtr("key")}, nil},
				{stringPtr("served"), nil, nil},
				{stringPtr("served"), &APIConfig{}, nil},
				{stringPtr("served"), &APIConfig{APIKey: stringPtr("")}, nil},
				{stringPtr("served"), &APIConfig{APIKey: stringPtr(" \t")}, nil},
				{stringPtr("served"), &APIConfig{APIKey: stringPtr("key"), Region: stringPtr("missing")}, nil},
				{stringPtr("served"), &APIConfig{APIKey: stringPtr("key")}, &EmbeddingConfig{Dimension: -1}},
			}
			for i, c := range cases {
				if v, err := d.Embed(c.name, []string{"a"}, c.api, c.cfg); err == nil || v != nil {
					t.Fatal(i, v, err)
				}
			}
			ctx, cancel := context.WithCancel(t.Context())
			cancel()
			if v, err := d.Embed(stringPtr("served"), []string{"a"}, &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil); !errors.Is(err, context.Canceled) || v != nil {
				t.Fatal(v, err)
			}
			if calls.Load() != 0 {
				t.Fatal("invalid input reached provider", calls.Load())
			}
			if provider == "GPUStack" {
				d = NewGPUStackModel(map[string]string{"default": srv.URL}, URLSuffix{})
				if v, err := d.Embed(stringPtr("served"), []string{"a"}, &APIConfig{APIKey: stringPtr("key")}, nil); err == nil || v != nil || calls.Load() != 0 {
					t.Fatal(v, err)
				}
			}
		})
	}
}

func TestAfter53GPUStackEmbeddingMalformed(t *testing.T) {
	cases := []string{
		`null`, `{}`, `{"data":null}`, `{"data":[]}`, `{"data":[{"index":0,"embedding":[0,1]}]}`,
		`{"data":[{"embedding":[0,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":null,"embedding":[0,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0.5,"embedding":[0,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0,"embedding":[0,1]},{"index":0,"embedding":[0,1]}]}`,
		`{"data":[{"index":-1,"embedding":[0,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":2,"embedding":[0,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0,"embedding":[]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0,"embedding":[null,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0,"embedding":[1e999,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0,"embedding":[NaN,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0,"embedding":["0",1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0,"embedding":[0]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0,"embedding":[0,1]},{"index":1,"embedding":[0,1]}],"error":"private-secret"}`,
		`{} {}`, `{"data":[`, strings.Repeat(" ", MaxMediaBytes+1),
	}
	for i, raw := range cases {
		t.Run(fmt.Sprint(i), func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, raw) }))
			v, err := after53GPUStack(t, srv.URL).Embed(stringPtr("served"), []string{"a", "b"}, &APIConfig{APIKey: stringPtr("key")}, &EmbeddingConfig{Dimension: 2})
			if err == nil || v != nil || strings.Contains(err.Error(), "private-secret") {
				t.Fatal(v, err)
			}
		})
	}
}

func TestAfter53GPUStackEmbeddingLaterBatchFailure(t *testing.T) {
	for _, failure := range []string{"HTTP", "dimension", "cancel", "deadline"} {
		t.Run(failure, func(t *testing.T) {
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			if failure == "deadline" {
				var stop context.CancelFunc
				ctx, stop = context.WithTimeout(t.Context(), 300*time.Millisecond)
				defer stop()
			}
			var calls atomic.Int32
			started, stopped := make(chan struct{}), make(chan struct{})
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body struct {
					Input []string `json:"input"`
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				if calls.Add(1) == 2 {
					switch failure {
					case "HTTP":
						w.WriteHeader(502)
						fmt.Fprint(w, "private-secret")
						return
					case "dimension":
						fmt.Fprint(w, `{"data":[{"index":0,"embedding":[0]}]}`)
						return
					default:
						close(started)
						select {
						case <-r.Context().Done():
						case <-time.After(2 * time.Second):
							t.Error("provider request leaked")
						}
						close(stopped)
						return
					}
				}
				data := []map[string]any{}
				for i := range body.Input {
					data = append(data, map[string]any{"index": i, "embedding": []float64{0, 1}})
				}
				json.NewEncoder(w).Encode(map[string]any{"data": data})
			}))
			if failure == "cancel" {
				go func() {
					select {
					case <-started:
						cancel()
					case <-ctx.Done():
					}
				}()
			}
			v, err := after53GPUStack(t, srv.URL).Embed(stringPtr("served"), make([]string, 33), &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil)
			if err == nil || v != nil || calls.Load() != 2 || strings.Contains(err.Error(), "private-secret") {
				t.Fatal(v, err, calls.Load())
			}
			if failure == "cancel" || failure == "deadline" {
				want := context.Canceled
				if failure == "deadline" {
					want = context.DeadlineExceeded
				}
				if !errors.Is(err, want) {
					t.Fatal(err)
				}
				select {
				case <-stopped:
				case <-time.After(time.Second):
					t.Fatal("provider request remains")
				}
			}
		})
	}
}

// A network-truncated response must never expose a partially decoded vector.
func TestAfter53GPUStackEmbeddingTruncated(t *testing.T) {
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", "9999")
		io.WriteString(w, `{"data":[{"index":0,"embedding":[0,1]}]}`)
	}))
	v, err := after53GPUStack(t, srv.URL).Embed(stringPtr("served"), []string{"a"}, &APIConfig{APIKey: stringPtr("key")}, nil)
	if err == nil || v != nil {
		t.Fatal(v, err)
	}
}
