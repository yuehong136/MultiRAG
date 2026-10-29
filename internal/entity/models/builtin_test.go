package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestBuiltinEmbeddingHTTPBatchesAndDimensions(t *testing.T) {
	for _, dimension := range []int{0, 3} {
		t.Run(strconv.Itoa(dimension), func(t *testing.T) {
			var calls atomic.Int64
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodPost || r.URL.Path != "/tei/embed" || r.Header.Get("Content-Type") != "application/json" {
					t.Error("TEI request method/path/content-type mismatch")
				}
				if r.Header.Get("Authorization") != "" || r.Header.Get("api-key") != "" {
					t.Error("Builtin forwarded provider credentials")
				}
				var body struct {
					Inputs     []string `json:"inputs"`
					Dimensions *int     `json:"dimensions"`
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				if dimension == 0 && body.Dimensions != nil || dimension > 0 && (body.Dimensions == nil || *body.Dimensions != dimension) {
					t.Error("dimension option changed", body.Dimensions)
				}
				callIndex := int(calls.Add(1) - 1)
				wantCount := 32
				if callIndex == 2 {
					wantCount = 1
				}
				if len(body.Inputs) != wantCount {
					t.Error("batch size", len(body.Inputs), wantCount)
				}
				vectors := make([][]float64, len(body.Inputs))
				for index, input := range body.Inputs {
					number, err := strconv.Atoi(input)
					if err != nil || number != callIndex*32+index {
						t.Error("input order changed", input)
					}
					vectors[index] = []float64{float64(number), 1, 0}
				}
				json.NewEncoder(w).Encode(vectors)
			}))
			model, err := NewBuiltinModel(srv.URL+"/tei/", "owned-model")
			if err != nil {
				t.Fatal(err)
			}
			texts := make([]string, 65)
			for index := range texts {
				texts[index] = strconv.Itoa(index)
			}
			data, err := model.Embed(stringPtr("owned-model"), texts, &APIConfig{APIKey: stringPtr("must-not-forward"), Context: t.Context()}, &EmbeddingConfig{Dimension: dimension})
			if err != nil || calls.Load() != 3 || len(data) != len(texts) {
				t.Fatal("batched embedding failed", calls.Load(), len(data), err)
			}
			for index, item := range data {
				if item.Index != index || len(item.Embedding) != 3 || item.Embedding[0] != float64(index) || item.Embedding[2] != 0 {
					t.Fatal("embedding index/value changed", index, item)
				}
			}
		})
	}
}

func TestBuiltinRejectsMalformedEmbeddingResponses(t *testing.T) {
	for name, raw := range map[string]string{
		"null response": "null", "missing vector": "[]", "null vector": "[null]", "empty vector": "[[]]",
		"null value": "[[1,null]]", "wrong count": "[[1,0],[2,0]]", "wrong dimension": "[[1]]",
		"overflow": "[[1e999,0]]", "wrong shape": `{"data":[[1,0]]}`, "trailing value": "[[1,0]] {}",
		"oversized": strings.Repeat(" ", MaxMediaBytes+1),
	} {
		t.Run(name, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, raw) }))
			model, err := NewBuiltinModel(srv.URL+"/embed", "owned-model")
			if err != nil {
				t.Fatal(err)
			}
			data, err := model.Embed(stringPtr("owned-model"), []string{"input"}, nil, &EmbeddingConfig{Dimension: 2})
			if err == nil || data != nil {
				t.Fatal("invalid embeddings accepted", len(data), err)
			}
		})
	}
}

func TestBuiltinRejectsCrossBatchDimensionChangesWithoutPartialResults(t *testing.T) {
	var calls atomic.Int64
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct{ Inputs []string }
		json.NewDecoder(r.Body).Decode(&body)
		callIndex := calls.Add(1) - 1
		vectors := make([][]float64, len(body.Inputs))
		for index := range vectors {
			vectors[index] = []float64{1, 0}
			if callIndex > 0 {
				vectors[index] = []float64{1}
			}
		}
		json.NewEncoder(w).Encode(vectors)
	}))
	model, err := NewBuiltinModel(srv.URL, "owned-model")
	if err != nil {
		t.Fatal(err)
	}
	data, err := model.Embed(stringPtr("owned-model"), make([]string, 33), nil, &EmbeddingConfig{Dimension: 0})
	if err == nil || data != nil || calls.Load() != 2 {
		t.Fatal("cross-batch mismatch returned partial results", len(data), calls.Load(), err)
	}
}

func TestBuiltinHTTPFailureAndRedirectArePrivate(t *testing.T) {
	var redirected atomic.Int64
	destination := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { redirected.Add(1) }))
	for _, status := range []int{http.StatusUnauthorized, http.StatusInternalServerError, http.StatusTemporaryRedirect} {
		t.Run(strconv.Itoa(status), func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Location", destination.URL+"/embed")
				w.WriteHeader(status)
				fmt.Fprint(w, "private-provider-payload")
			}))
			model, err := NewBuiltinModel(srv.URL, "owned-model")
			if err != nil {
				t.Fatal(err)
			}
			data, err := model.Embed(stringPtr("owned-model"), []string{"input"}, nil, nil)
			if data != nil || err == nil || strings.Contains(err.Error(), "private-provider-payload") {
				t.Fatal("HTTP error lost or leaked", data, err)
			}
		})
	}
	if redirected.Load() != 0 {
		t.Fatal("Builtin followed redirect")
	}
}

func TestBuiltinInvalidCallsHaveNoHTTPAndUnsupportedFails(t *testing.T) {
	var calls atomic.Int64
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
	model, err := NewBuiltinModel(srv.URL, "owned-model")
	if err != nil {
		t.Fatal(err)
	}
	for _, tt := range []struct {
		name   *string
		config *EmbeddingConfig
	}{
		{nil, nil}, {stringPtr("other-model"), nil}, {stringPtr("owned-model"), &EmbeddingConfig{Dimension: -1}},
	} {
		if data, err := model.Embed(tt.name, []string{"input"}, nil, tt.config); err == nil || data != nil {
			t.Fatal("invalid request accepted", data, err)
		}
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if data, err := model.Embed(stringPtr("owned-model"), []string{"input"}, &APIConfig{Context: ctx}, nil); !errors.Is(err, context.Canceled) || data != nil {
		t.Fatal("pre-canceled request accepted", data, err)
	}
	if data, err := model.Embed(stringPtr("owned-model"), nil, nil, nil); err != nil || data == nil || len(data) != 0 {
		t.Fatal("empty input contract changed", data, err)
	}
	if response, err := model.ChatWithMessages("owned-model", nil, []Message{{Role: "user", Content: "q"}}, nil); response != nil || err == nil {
		t.Fatal("unsupported chat succeeded")
	}
	if response, err := model.Rerank(stringPtr("owned-model"), "q", []string{"a"}, nil, nil); response != nil || err == nil {
		t.Fatal("unsupported rerank succeeded")
	}
	if err := model.ChatStreamlyWithMessages("owned-model", []Message{{Role: "user", Content: "q"}}, nil, nil, func(*string, *string) error { t.Error("unsupported stream emitted output"); return nil }); err == nil {
		t.Fatal("unsupported stream succeeded")
	}
	if _, err := model.Balance(nil); err == nil {
		t.Fatal("unsupported balance succeeded")
	}
	if calls.Load() != 0 {
		t.Fatal("invalid/unsupported request contacted TEI", calls.Load())
	}
}

func TestBuiltinConfigurationAndDiscovery(t *testing.T) {
	for _, endpoint := range []string{"", "ftp://example.test", "http://", "http://user:pass@example.test", "http://example.test?q=x", "http://example.test/#fragment"} {
		if model, err := NewBuiltinModel(endpoint, "owned-model"); model != nil || err == nil {
			t.Fatal("invalid endpoint accepted", endpoint)
		}
	}
	if model, err := NewBuiltinModel("http://example.test", " "); model != nil || err == nil {
		t.Fatal("missing model accepted")
	}
	var calls atomic.Int64
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/embed" {
			t.Error("duplicated embed suffix", r.URL.Path)
		}
		fmt.Fprint(w, "[[0,1]]")
	}))
	model, err := NewBuiltinModel(srv.URL+"/embed", "owned-model")
	if err != nil {
		t.Fatal(err)
	}
	if model.Name() != "Builtin" {
		t.Fatal("wrong provider name", model.Name())
	}
	list, err := model.ListModels(nil)
	if err != nil || len(list) != 1 || list[0] != "owned-model" {
		t.Fatal("model discovery", list, err)
	}
	list[0] = "poison"
	list, _ = model.ListModels(nil)
	if list[0] != "owned-model" {
		t.Fatal("discovery leaked mutable model state")
	}
	if err := model.CheckConnection(nil); err != nil || calls.Load() != 1 {
		t.Fatal("connection did not perform configured TEI embedding", calls.Load(), err)
	}
}

type builtinReadObserver struct {
	io.ReadCloser
	started chan struct{}
	once    sync.Once
}

func (b *builtinReadObserver) Read(data []byte) (int, error) {
	b.once.Do(func() { close(b.started) })
	return b.ReadCloser.Read(data)
}

type builtinObserveTransport struct {
	base    http.RoundTripper
	started chan struct{}
}

func (b builtinObserveTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	response, err := b.base.RoundTrip(request)
	if err == nil {
		response.Body = &builtinReadObserver{ReadCloser: response.Body, started: b.started}
	}
	return response, err
}

func TestBuiltinCancellationDuringResponseRead(t *testing.T) {
	stopped := make(chan struct{})
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer close(stopped)
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, "[[")
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}))
	model, err := NewBuiltinModel(srv.URL, "owned-model")
	if err != nil {
		t.Fatal(err)
	}
	reading := make(chan struct{})
	model.httpClient.Transport = builtinObserveTransport{base: model.httpClient.Transport, started: reading}
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		data, err := model.Embed(stringPtr("owned-model"), []string{"input"}, &APIConfig{Context: ctx}, nil)
		if data != nil {
			done <- fmt.Errorf("canceled request returned data")
			return
		}
		done <- err
	}()
	select {
	case <-reading:
	case <-time.After(3 * time.Second):
		t.Fatal("client did not begin reading TEI response")
	}
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatal("response-read cancellation cause lost", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("canceled TEI request did not return")
	}
	select {
	case <-stopped:
	case <-time.After(3 * time.Second):
		t.Fatal("canceled TEI response/socket did not close")
	}
}
