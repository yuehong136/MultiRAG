package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"

	"reflect"
	"strconv"
	"sync/atomic"
	"testing"
	"time"
)

func TestSiliconFlowEmbeddingsHTTP(t *testing.T) { testHostedEmbeddingsHTTP(t, "SiliconFlow") }

func TestSiliconFlowEmbeddingBatchBoundariesAndDimensions(t *testing.T) {
	for _, size := range []int{0, 1, 32, 33, 65} {
		t.Run(strconv.Itoa(size), func(t *testing.T) {
			var batches []int
			server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body struct {
					Model      string
					Input      []string
					Dimensions int
					Query      *bool
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if r.Header.Get("Authorization") != "Bearer fixture" {
					t.Error("embedding request lost configuration", body, r.URL.Path)
				}
				batches = append(batches, len(body.Input))
				data := make([]map[string]any, 0, len(body.Input))
				for i := len(body.Input) - 1; i >= 0; i-- {
					value, err := strconv.Atoi(body.Input[i])
					if err != nil {
						t.Error(err)
					}
					data = append(data, map[string]any{"index": i, "embedding": []float64{float64(value), 0}})
				}
				// Real SiliconFlow responses carry an object string, not a string array.
				json.NewEncoder(w).Encode(map[string]any{"object": "list", "data": data})
			}))
			defer server.Close()
			driver := NewSiliconflowModel(map[string]string{"default": server.URL + "/v1"}, URLSuffix{Embedding: "embeddings"})
			api := &APIConfig{ApiKey: stringPtr("fixture")}
			config := &EmbeddingConfig{Dimension: 2}
			model := NewEmbeddingModel(driver, stringPtr("embed"), api, 0)

			texts, want := make([]string, size), make([][]float64, size)
			for i := range texts {
				texts[i], want[i] = strconv.Itoa(i), []float64{float64(i), 0}
			}
			vectors, err := embeddingVectorsForTest(model.ModelDriver.Embed(model.ModelName, texts, api,
				config,
			))
			if size > 32 {
				if err == nil || vectors != nil || len(batches) != 0 {
					t.Fatal("oversized input reached provider", vectors, batches, err)
				}
			} else {
				wantBatches := []int(nil)
				if size > 0 {
					wantBatches = []int{size}
				}
				if err != nil || !reflect.DeepEqual(vectors, want) || !reflect.DeepEqual(batches, wantBatches) {
					t.Fatal(vectors, batches, err)
				}
			}
			if api.Context != nil {
				t.Fatal(vectors, batches, err)
			}
		})
	}
}

func TestSiliconFlowEmbeddingDimensionValidation(t *testing.T) {
	var calls atomic.Int32
	server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		fmt.Fprint(w, `{"data":[{"index":0,"embedding":[1]}]}`)
	}))
	defer server.Close()
	driver := NewSiliconflowModel(map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings"})
	for _, dimension := range []int{-1, 2} {
		vectors, err := embeddingVectorsForTest(driver.Embed(stringPtr("model"), []string{"a"}, &APIConfig{ApiKey: stringPtr("fixture")}, &EmbeddingConfig{Dimension: dimension}))
		if err == nil || vectors != nil {
			t.Fatal("invalid dimension yielded success", dimension, vectors, err)
		}
	}
	if calls.Load() != 1 {
		t.Fatal("negative dimensions reached provider", calls.Load())
	}
}

func TestSiliconFlowEmbeddingLaterBatchFailure(t *testing.T) {
	for _, failure := range []string{"http", "duplicate", "dimension", "null"} {
		t.Run(failure, func(t *testing.T) {
			var calls atomic.Int32
			server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				call := calls.Add(1)
				var body struct{ Input []string }
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if call == 1 && failure == "http" {
					w.WriteHeader(http.StatusServiceUnavailable)
					return
				}
				data := make([]map[string]any, len(body.Input))
				for i := range data {
					index, vector := i, any([]float64{0, 1})
					if call == 1 && i > 0 {
						switch failure {
						case "duplicate":
							index = 0
						case "dimension":
							vector = []float64{1}
						case "null":
							vector = []any{nil, 1}
						}
					}
					data[i] = map[string]any{"index": index, "embedding": vector}
				}
				json.NewEncoder(w).Encode(map[string]any{"data": data})
			}))
			defer server.Close()
			driver := NewSiliconflowModel(map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings"})
			vectors, err := embeddingVectorsForTest(driver.Embed(stringPtr("model"), make([]string, 32), &APIConfig{ApiKey: stringPtr("fixture")}, nil))
			if err == nil || vectors != nil || calls.Load() != 1 {
				t.Fatal("later failure returned partial success or issued another batch", vectors, err, calls.Load())
			}
		})
	}
}

func TestSiliconFlowEmbeddingLaterBatchCancellation(t *testing.T) {
	started := make(chan struct{})
	var calls atomic.Int32
	server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		call := calls.Add(1)
		var body struct{ Input []string }
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		io.Copy(io.Discard, r.Body)
		r.Body.Close()
		if call == 1 {
			close(started)
			<-r.Context().Done()
			return
		}
		data := make([]map[string]any, len(body.Input))
		for i := range data {
			data[i] = map[string]any{"index": i, "embedding": []float64{0, 1}}
		}
		json.NewEncoder(w).Encode(map[string]any{"data": data})
	}))
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	driver := NewSiliconflowModel(map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings"})
	result := make(chan error, 1)
	go func() {
		vectors, err := embeddingVectorsForTest(driver.Embed(stringPtr("model"), make([]string, 32), &APIConfig{ApiKey: stringPtr("fixture"), Context: ctx}, nil))
		if vectors != nil {
			result <- fmt.Errorf("cancellation returned partial embeddings")
			return
		}
		result <- err
	}()
	select {
	case <-started:
		cancel()
	case <-ctx.Done():
		t.Fatal("second batch did not reach HTTP boundary")
	}
	select {
	case err := <-result:
		if !errors.Is(err, context.Canceled) || calls.Load() != 1 {
			t.Fatal(err, calls.Load())
		}
	case <-time.After(3 * time.Second):
		t.Fatal("later batch ignored cancellation")
	}
}
