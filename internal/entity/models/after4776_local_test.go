package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestAfter4776OllamaNativeEmbedding(t *testing.T) {
	var calls atomic.Int32
	server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/api/embed" || r.Method != "POST" || r.Header.Get("Authorization") != "" {
			t.Error("native request/auth", r.URL.Path)
		}
		var body struct {
			Model      string   `json:"model"`
			Input      []string `json:"input"`
			Dimensions int      `json:"dimensions"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body.Model != "m" || body.Dimensions != 2 || len(body.Input) > 32 {
			t.Error(body)
		}
		vectors := make([][]float64, len(body.Input))
		for i := range vectors {
			vectors[i] = []float64{0, 1}
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"embeddings": vectors})
	}))
	defer server.Close()
	urls := map[string]string{"default": server.URL}
	driver := NewOllamaModel(urls, URLSuffix{Embedding: "api/embed"})
	urls["default"] = "http://unused.invalid"
	name := "m"
	texts := make([]string, 35)
	for i := range texts {
		texts[i] = "text"
	}
	result, err := driver.Embed(&name, texts, nil, &EmbeddingConfig{Dimension: 2, Query: true})
	if err != nil || len(result) != 35 || calls.Load() != 2 {
		t.Fatal(result, err, calls.Load())
	}
	for i, v := range result {
		if v.Index != i || !reflect.DeepEqual(v.Embedding, []float64{0, 1}) {
			t.Fatal(v)
		}
	}
	if empty, err := driver.Embed(nil, nil, nil, nil); err != nil || empty == nil || len(empty) != 0 {
		t.Fatal(empty, err)
	}
}
func TestAfter4776OllamaRawFailures(t *testing.T) {
	for _, body := range []string{`{}`, `{"embeddings":null}`, `{"embeddings":[]}`, `{"embeddings":[[null,1]]}`, `{"embeddings":[[0]]}`, `{"embeddings":[[0,1],[0,1]]}`, `{"embeddings":[[0,1]],"error":"private-secret"}`, `{"embeddings":[[0,1]]} {}`, `{"embeddings":[[1e400,1]]}`} {
		t.Run(body, func(t *testing.T) {
			server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
			defer server.Close()
			name := "m"
			driver := NewOllamaModel(map[string]string{"default": server.URL}, URLSuffix{Embedding: "api/embed"})
			if value, err := driver.Embed(&name, []string{"x"}, nil, &EmbeddingConfig{Dimension: 2}); err == nil || value != nil || strings.Contains(err.Error(), "private-secret") {
				t.Fatal(value, err)
			}
		})
	}
	for _, body := range []string{`{"models":[{"name":" "}]}`, `{"models":[{"name":"m"},{"name":"m"}]}`, `{"models":[{"name":" m"}]}`, `{"models":[]} {}`} {
		server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
		driver := NewOllamaModel(map[string]string{"default": server.URL}, URLSuffix{Models: "api/tags"})
		if names, err := driver.ListModels(nil); err == nil || names != nil {
			t.Fatal(names, err)
		}
		server.Close()
	}
}
func TestAfter4776OCRPreflightNoHTTP(t *testing.T) {
	var calls atomic.Int32
	server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); fmt.Fprint(w, `{}`) }))
	defer server.Close()
	key, name, blank := "controlled-key", "ocr-model", " "
	for _, driver := range []OCRDriver{NewBaiduModel(map[string]string{"default": server.URL}, URLSuffix{OCR: "ocr"}), NewZhipuAIModel(map[string]string{"default": server.URL}, URLSuffix{OCR: "ocr"})} {
		for _, input := range []struct {
			name    *string
			content []byte
			api     *APIConfig
		}{
			{&name, nil, &APIConfig{APIKey: &key}}, {nil, []byte("image"), &APIConfig{APIKey: &key}}, {&blank, []byte("image"), &APIConfig{APIKey: &key}}, {&name, []byte("image"), nil}, {&name, []byte("image"), &APIConfig{}}, {&name, []byte("image"), &APIConfig{APIKey: &blank}},
		} {
			if result, err := driver.OCRFile(input.name, input.content, input.api, nil); err == nil || result != nil {
				t.Fatal(result, err)
			}
		}
	}
	if calls.Load() != 0 {
		t.Fatal("invalid OCR inputs reached HTTP", calls.Load())
	}
}
func TestAfter4776OllamaStreamCancelBetweenChannels(t *testing.T) {
	server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintln(w, `{"message":{"thinking":"thought","content":"answer"},"done":true}`)
	}))
	defer server.Close()
	driver := NewOllamaModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "api/chat"})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	content := false
	err := driver.ChatStreamlyWithMessages("m", []Message{{Role: "user", Content: "q"}}, &APIConfig{Context: ctx}, nil, func(c, r *string) error {
		if r != nil {
			cancel()
		}
		if c != nil {
			content = true
		}
		return nil
	})
	if !errors.Is(err, context.Canceled) || content {
		t.Fatal(err, content)
	}
}

func TestAfter4776OllamaIdleStream(t *testing.T) {
	server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.(http.Flusher).Flush(); <-r.Context().Done() }))
	defer server.Close()
	driver := NewOllamaModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "api/chat"})
	driver.streamIdleTimeout = 40 * time.Millisecond
	err := driver.ChatStreamlyWithMessages("m", []Message{{Role: "user", Content: "q"}}, nil, nil, func(*string, *string) error { return nil })
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
}

func TestAfter4776OllamaLargeNDJSONAndEmptyCatalog(t *testing.T) {
	answer := strings.Repeat("x", 100*1024)
	server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "GET" {
			fmt.Fprint(w, `{"models":[]}`)
			return
		}
		fmt.Fprintf(w, "\n{\"message\":{\"content\":%q},\"done\":true}\n", answer)
	}))
	driver := NewOllamaModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "api/chat", Models: "api/tags"})
	var got string
	err := driver.ChatStreamlyWithMessages("m", []Message{{Role: "user", Content: "q"}}, nil, nil, func(c, r *string) error {
		if c != nil && *c != "[DONE]" {
			got += *c
		}
		return nil
	})
	if err != nil || got != answer {
		t.Fatal("large NDJSON lost", len(got), err)
	}
	if names, err := driver.ListModels(nil); err != nil || names == nil || len(names) != 0 {
		t.Fatal(names, err)
	}
}
