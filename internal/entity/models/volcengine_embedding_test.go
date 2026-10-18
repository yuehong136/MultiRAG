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
	"sync/atomic"
	"testing"
	"time"
)

func TestVolcEngineMultimodalEmbeddingHTTP(t *testing.T) {
	for _, instance := range []string{"one", "two"} {
		t.Run(instance, func(t *testing.T) {
			t.Parallel()
			var inputs []string
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodPost || r.URL.Path != "/ark/embeddings/multimodal" || r.Header.Get("Authorization") != "Bearer "+instance {
					t.Error("wrong instance endpoint or key")
				}
				var body struct {
					Model          string
					EncodingFormat string `json:"encoding_format"`
					Input          []struct{ Type, Text string }
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if body.Model != "doubao-embedding-vision-250615" || body.EncodingFormat != "float" || len(body.Input) != 1 || body.Input[0].Type != "text" {
					t.Error("invalid multimodal embedding body", body)
					w.WriteHeader(400)
					return
				}
				inputs = append(inputs, body.Input[0].Text)
				fmt.Fprintf(w, `{"data":{"embedding":[%d,0],"object":"embedding"}}`, len(inputs))
			}))
			defer server.Close()
			driver, _ := NewModelFactory().CreateModelDriver("VolcEngine", map[string]string{"default": server.URL + "/ark/"}, URLSuffix{Embedding: "embeddings/multimodal"})
			name := "doubao-embedding-vision-250615"
			api := &APIConfig{APIKey: &instance, Region: stringPtr("")}
			vectors, err := NewEmbeddingModel(driver, &name, api).Encode([]string{"first", "second"})
			if err != nil || !reflect.DeepEqual(vectors, [][]float64{{1, 0}, {2, 0}}) || !reflect.DeepEqual(inputs, []string{"first", "second"}) {
				t.Fatal(vectors, inputs, err)
			}
		})
	}
}

func TestVolcEngineEmbeddingMalformedAndPartialFailureHTTP(t *testing.T) {
	for _, test := range []struct {
		body   string
		status int
	}{
		{`{}`, 200}, {`{"data":null}`, 200}, {`{"data":{"embedding":[]}}`, 200},
		{`{"data":{"embedding":[1e999]}}`, 200}, {`{"data":{"embedding":["bad"]}}`, 200},
		{`{"data":{"embedding":[1]}}`, 200},
		{`{"data":{"embedding":[1]},"error":"private-body"}`, 200}, {`invalid`, 200}, {`private-body`, 429},
	} {
		t.Run(test.body, func(t *testing.T) {
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if calls.Add(1) == 1 {
					fmt.Fprint(w, `{"data":{"embedding":[1,2]}}`)
					return
				}
				w.WriteHeader(test.status)
				fmt.Fprint(w, test.body)
			}))
			defer server.Close()
			driver := NewVolcEngine(map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings/multimodal"})
			name := "m"
			vectors, err := driver.Encode(&name, []string{"first", "second", "third"}, &APIConfig{APIKey: stringPtr("key")}, nil)
			if err == nil || vectors != nil || calls.Load() != 2 || strings.Contains(err.Error(), "private-body") {
				t.Fatal(vectors, err, calls.Load())
			}
		})
	}
}

func TestVolcEngineEmbeddingPreflightAndCancellationHTTP(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		fmt.Fprint(w, `{"data":{"embedding":[1,2]}}`)
	}))
	defer server.Close()
	driver := NewVolcEngine(map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings/multimodal"})
	name := "m"
	if vectors, err := driver.Encode(nil, nil, nil, nil); err != nil || vectors == nil || len(vectors) != 0 {
		t.Fatal(vectors, err)
	}
	for _, test := range []struct {
		name *string
		api  *APIConfig
	}{
		{nil, &APIConfig{APIKey: stringPtr("key")}}, {stringPtr(" "), &APIConfig{APIKey: stringPtr("key")}},
		{&name, nil}, {&name, &APIConfig{}}, {&name, &APIConfig{APIKey: stringPtr(" ")}},
		{&name, &APIConfig{APIKey: stringPtr("key"), Region: stringPtr("missing")}},
	} {
		if vectors, err := driver.Encode(test.name, []string{"q"}, test.api, nil); err == nil || vectors != nil {
			t.Fatal(vectors, err)
		}
	}
	if calls.Load() != 0 {
		t.Fatal("invalid input reached HTTP")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := driver.Encode(&name, []string{"q"}, &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	stopped := make(chan struct{})
	ctx, cancel = context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	blocked := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer close(stopped)
		var body any
		_ = json.NewDecoder(r.Body).Decode(&body)
		r.Body.Close()
		w.WriteHeader(200)
		w.(http.Flusher).Flush()
		cancel()
		<-r.Context().Done()
	}))
	defer blocked.Close()
	driver = NewVolcEngine(map[string]string{"default": blocked.URL}, URLSuffix{Embedding: "embeddings/multimodal"})
	if _, err := driver.Encode(&name, []string{"q"}, &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	select {
	case <-stopped:
	case <-time.After(time.Second):
		t.Fatal("HTTP body not released")
	}
}

func TestSiliconFlowDiscoveryNilConfigAndEmptyRegionHTTP(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/models" || r.Header.Get("Authorization") != "Bearer key" {
			t.Error("incorrect discovery endpoint/key")
		}
		fmt.Fprint(w, `{"data":[{"id":"m"}]}`)
	}))
	defer server.Close()
	driver := NewSiliconFlowModel(map[string]string{"default": server.URL}, URLSuffix{Models: "models"})
	for _, api := range []*APIConfig{nil, {}, {APIKey: stringPtr("")}} {
		if _, err := driver.ListModels(api); err == nil {
			t.Fatal("missing key accepted")
		}
		if err := driver.CheckConnection(api); err == nil {
			t.Fatal("missing key connection succeeded")
		}
	}
	if calls.Load() != 0 {
		t.Fatal("missing key reached HTTP")
	}
	if names, err := driver.ListModels(&APIConfig{APIKey: stringPtr("key"), Region: stringPtr("")}); err != nil || !reflect.DeepEqual(names, []string{"m"}) {
		t.Fatal(names, err)
	}
}
