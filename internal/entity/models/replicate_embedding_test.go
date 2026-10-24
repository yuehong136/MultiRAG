package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestReplicateEmbeddingPredictionContract(t *testing.T) {
	for _, versioned := range []bool{false, true} {
		for _, count := range []int{1, 3} {
			for _, objects := range []bool{false, true} {
				t.Run(fmt.Sprint(versioned, count, objects), func(t *testing.T) {
					name := "owner/model"
					if versioned {
						name += ":" + strings.Repeat("a", 64)
					}
					texts := []string{"a", "b", "c"}[:count]
					calls := 0
					srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						if r.Header.Get("Authorization") != "Bearer key" {
							t.Error("auth lost")
						}
						calls++
						if r.Method == "POST" {
							path := "/v1/models/owner/model/predictions"
							if versioned {
								path = "/v1/predictions"
							}
							var body struct {
								Input   map[string]any
								Version string
								Stream  bool
							}
							if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
								t.Error(err)
							}
							if r.URL.Path != path || r.Header.Get("Prefer") != "wait=60" || body.Stream || versioned && body.Version != name || !versioned && body.Version != "" {
								t.Error("route/version lost", body)
							}
							if count == 1 {
								if body.Input["text"] != "a" || len(body.Input) != 1 {
									t.Error(body)
								}
							} else {
								encoded, _ := json.Marshal(texts)
								if body.Input["text_batch"] != string(encoded) || len(body.Input) != 1 {
									t.Error(body)
								}
							}
							fmt.Fprintf(w, `{"id":"pred-1","status":"processing","urls":{"get":"http://%s/v1/predictions/pred-1"}}`, r.Host)
						} else {
							if r.Method != "GET" || r.ContentLength != 0 || r.URL.Path != "/v1/predictions/pred-1" {
								t.Error("bad poll")
							}
							output := make([]any, count)
							for i := range output {
								v := []float64{0, float64(i + 1)}
								if objects {
									output[i] = map[string]any{"embedding": v}
								} else {
									output[i] = v
								}
							}
							json.NewEncoder(w).Encode(map[string]any{"id": "pred-1", "status": "succeeded", "output": output})
						}
					}))
					defer srv.Close()
					result, err := newReplicateTestModel(srv.URL).Embed(&name, texts, &APIConfig{APIKey: stringPtr("key")}, &EmbeddingConfig{Dimension: 2})
					if err != nil || len(result) != count || calls != 2 {
						t.Fatal(result, err, calls)
					}
					for i, item := range result {
						if item.Index != i || !reflect.DeepEqual(item.Embedding, []float64{0, float64(i + 1)}) {
							t.Fatal(item)
						}
					}
				})
			}
		}
	}
}

func TestReplicateEmbeddingRejectsInvalidRawOutput(t *testing.T) {
	for _, raw := range []string{`null`, `{}`, `[]`, `[null]`, `[{}]`, `[{"embedding":null}]`, `[{"embedding":[]}]`, `[[null,1]]`, `[["0",1]]`, `[[0]]`, `[[0,1],[0,1]]`, `[[NaN,1]]`, `[[1e9999,1]]`} {
		t.Run(raw, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				fmt.Fprintf(w, `{"id":"pred-1","status":"succeeded","output":%s}`, raw)
			}))
			defer srv.Close()
			name := "owner/model"
			result, err := newReplicateTestModel(srv.URL).Embed(&name, []string{"q"}, &APIConfig{APIKey: stringPtr("key")}, &EmbeddingConfig{Dimension: 2})
			if err == nil || result != nil {
				t.Fatal(result, err)
			}
		})
	}
	// Consistency is checked even when the caller does not request a dimension.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"id":"pred-1","status":"succeeded","output":[[0,1],[0]]}`)
	}))
	defer srv.Close()
	name := "owner/model"
	if data, err := newReplicateTestModel(srv.URL).Embed(&name, []string{"q", "r"}, &APIConfig{APIKey: stringPtr("key")}, nil); err == nil || data != nil {
		t.Fatal(data, err)
	}
}

func TestReplicateEmbeddingCancellationAndEmpty(t *testing.T) {
	m := newReplicateTestModel("http://127.0.0.1:1")
	if data, err := m.Embed(nil, nil, nil, nil); err != nil || data == nil || len(data) != 0 {
		t.Fatal(data, err)
	}
	started, closed := make(chan struct{}), make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "POST" {
			fmt.Fprintf(w, `{"id":"pred-1","status":"starting","urls":{"get":"http://%s/v1/predictions/pred-1"}}`, r.Host)
			return
		}
		close(started)
		<-r.Context().Done()
		close(closed)
	}))
	defer srv.Close()
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	name := "owner/model"
	done := make(chan error, 1)
	go func() {
		data, err := newReplicateTestModel(srv.URL).Embed(&name, []string{"q"}, &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil)
		if data != nil {
			t.Error("partial result")
		}
		done <- err
	}()
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("poll not started")
	}
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	select {
	case <-closed:
	case <-time.After(time.Second):
		t.Fatal("poll leaked")
	}
}
