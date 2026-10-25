package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestAfterf9EmbeddingBatchAndProviderIdentity(t *testing.T) {
	for _, provider := range []string{"Astraflow", "HunYuan", "TokenHub"} {
		t.Run(provider, func(t *testing.T) {
			var calls atomic.Int32
			srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				var body struct {
					Model      string   `json:"model"`
					Input      []string `json:"input"`
					Dimensions int      `json:"dimensions"`
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				if r.Method != "POST" || r.URL.Path != "/embeddings" || r.Header.Get("Authorization") != "Bearer key" || body.Model != provider+"-served" || body.Dimensions != 2 {
					t.Error("wrong provider/model/request", r.URL, body)
				}
				data := []map[string]any{}
				for i := len(body.Input) - 1; i >= 0; i-- {
					data = append(data, map[string]any{"index": i, "embedding": []float64{float64(len(body.Input[i])), 0}})
				}
				json.NewEncoder(w).Encode(map[string]any{"data": data, "code": 0})
			}))
			defer srv.Close()
			driver := afterf9Driver(t, provider, srv.URL)
			bound := NewEmbeddingModel(driver, stringPtr(provider+"-served"), &APIConfig{APIKey: stringPtr("key")})
			bound.EmbeddingConfig = &EmbeddingConfig{Dimension: 2}
			texts := make([]string, 33)
			for i := range texts {
				texts[i] = strings.Repeat("x", i+1)
			}
			data, err := bound.Embed(texts)
			if err != nil || len(data) != 33 || calls.Load() != 2 {
				t.Fatal(data, err, calls.Load())
			}
			for i, item := range data {
				if item.Index != i || item.Embedding[0] != float64(i+1) || item.Embedding[1] != 0 {
					t.Fatal(item)
				}
			}
			if empty, err := driver.Embed(nil, nil, nil, nil); err != nil || empty == nil || len(empty) != 0 || calls.Load() != 2 {
				t.Fatal(empty, err)
			}
		})
	}
}

func TestAfterf9EmbeddingRejectsRawMalformed(t *testing.T) {
	for _, provider := range []string{"Astraflow", "HunYuan", "TokenHub"} {
		for i, body := range []string{`null`, `{}`, `{"data":null}`, `{"data":[]}`, `{"data":[{"index":0,"embedding":[0,1]}]}`, `{"data":[{"embedding":[0,1]},{"index":1,"embedding":[0,1]}]}`, `{"data":[{"index":0,"embedding":[0,1]},{"index":0,"embedding":[0,1]}]}`, `{"data":[{"index":-1,"embedding":[0,1]},{"index":1,"embedding":[0,1]}]}`, `{"data":[{"index":2,"embedding":[0,1]},{"index":1,"embedding":[0,1]}]}`, `{"data":[{"index":0.5,"embedding":[0,1]},{"index":1,"embedding":[0,1]}]}`, `{"data":[{"index":0,"embedding":[0]},{"index":1,"embedding":[0,1]}]}`, `{"data":[{"index":0,"embedding":[null,1]},{"index":1,"embedding":[0,1]}]}`, `{"data":[{"index":0,"embedding":[1e999,1]},{"index":1,"embedding":[0,1]}]}`, `{"data":[{"index":0,"embedding":["0",1]},{"index":1,"embedding":[0,1]}]}`, `{"data":[{"index":0,"embedding":[0,1]},{"index":1,"embedding":[0,1]}],"code":1}`, `{"data":[{"index":0,"embedding":[0,1]},{"index":1,"embedding":[0,1]}],"code":null}`, `{"error":"private-secret"}`, `{} {}`} {
			t.Run(fmt.Sprintf("%s/%d", provider, i), func(t *testing.T) {
				srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
				defer srv.Close()
				result, err := afterf9Driver(t, provider, srv.URL).Embed(stringPtr("served"), []string{"a", "b"}, &APIConfig{APIKey: stringPtr("key")}, &EmbeddingConfig{Dimension: 2})
				if err == nil || result != nil || strings.Contains(err.Error(), "private-secret") {
					t.Fatal(result, err)
				}
			})
		}
	}
}

func TestAfterf9VectorsCancelHTTP(t *testing.T) {
	for _, provider := range []string{"Astraflow", "HunYuan", "TokenHub"} {
		for _, method := range []string{"embedding", "rerank"} {
			if method == "rerank" && provider != "Astraflow" {
				continue
			}
			t.Run(provider+method, func(t *testing.T) {
				started, stopped := make(chan struct{}), make(chan struct{})
				srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					io.Copy(io.Discard, r.Body)
					close(started)
					select {
					case <-r.Context().Done():
					case <-time.After(2 * time.Second):
						t.Error("provider not canceled")
					}
					close(stopped)
				}))
				defer srv.Close()
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				go func() { <-started; cancel() }()
				d := afterf9Driver(t, provider, srv.URL)
				api := &APIConfig{APIKey: stringPtr("key"), Context: ctx}
				var err error
				if method == "embedding" {
					_, err = d.Embed(stringPtr("served"), []string{"a"}, api, nil)
				} else {
					_, err = d.Rerank(stringPtr("served"), "q", []string{"a"}, api, nil)
				}
				if !errors.Is(err, context.Canceled) {
					t.Fatal(err)
				}
				select {
				case <-stopped:
				case <-time.After(time.Second):
					t.Fatal("provider request leaked")
				}
			})
		}
	}
}

func TestAfterf9AstraflowRerankMalformed(t *testing.T) {
	for _, body := range []string{`{}`, `{"results":[]}`, `{"results":[{"index":0}]}`, `{"results":[{"relevance_score":0}]}`, `{"results":[{"index":0,"relevance_score":null}]}`, `{"results":[{"index":1,"relevance_score":0}]}`, `{"results":[{"index":0,"relevance_score":1e999}]}`, `{"results":[{"index":0,"relevance_score":0}],"error":"private-secret"}`} {
		t.Run(body, func(t *testing.T) {
			srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
			defer srv.Close()
			result, err := afterf9Driver(t, "Astraflow", srv.URL).Rerank(stringPtr("served"), "q", []string{"a"}, &APIConfig{APIKey: stringPtr("key")}, nil)
			if err == nil || result != nil || strings.Contains(err.Error(), "private-secret") {
				t.Fatal(result, err)
			}
		})
	}
}
