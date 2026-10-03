package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

func TestSiliconFlowBoundEmbeddingsBatchOrderAndRerank(t *testing.T) {
	var batches []int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer key" {
			t.Error("wrong credential")
		}
		var body struct {
			Model string   `json:"model"`
			Input []string `json:"input"`
			TopN  int      `json:"top_n"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body.Model != "Qwen/model" {
			t.Errorf("model = %q", body.Model)
		}
		switch r.URL.Path {
		case "/embeddings":
			batches = append(batches, len(body.Input))
			var data []map[string]interface{}
			for i := len(body.Input) - 1; i >= 0; i-- {
				data = append(data, map[string]interface{}{"index": i, "embedding": []float64{float64(i + 1), 2}})
			}
			json.NewEncoder(w).Encode(map[string]interface{}{"object": "list", "data": data})
		case "/rerank":
			if body.TopN != 2 {
				t.Errorf("top_n = %d", body.TopN)
			}
			fmt.Fprint(w, `{"results":[{"index":1,"relevance_score":0.9},{"index":0,"relevance_score":0.1}]}`)
		default:
			t.Errorf("path = %s", r.URL.Path)
		}
	}))
	defer server.Close()
	driver := NewSiliconFlowModel(map[string]string{"default": "http://invalid.invalid", "fixture": server.URL}, URLSuffix{Embedding: "embeddings", Rerank: "rerank"})
	key, name, region := "key", "Qwen/model", "fixture"
	config := &APIConfig{APIKey: &key, Region: &region}
	model := NewEmbeddingModel(driver, &name, config)
	vectors, err := model.Encode(make([]string, 33))
	if err != nil || len(vectors) != 33 || !reflect.DeepEqual(vectors[0], []float64{1, 2}) || !reflect.DeepEqual(batches, []int{32, 1}) {
		t.Fatalf("vectors=%v batches=%v error=%v", vectors, batches, err)
	}
	query, err := model.Encode([]string{"question"})
	if err != nil || !reflect.DeepEqual(query, [][]float64{{1, 2}}) {
		t.Fatalf("query=%v error=%v", query, err)
	}
	rerank := NewRerankModel(driver, &name, config)
	scores, err := rerank.Rerank("q", []string{"first", "second"})
	if err != nil || !reflect.DeepEqual(scores, []float64{0.1, 0.9}) {
		t.Fatalf("scores=%v error=%v", scores, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	config.Context = ctx
	if _, err := model.Encode([]string{"q"}); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancel error=%v", err)
	}
}

func TestSiliconFlowMalformedEmbeddingAndRerank(t *testing.T) {
	tests := []struct{ body, path string }{
		{`{"data":[]}`, "embeddings"},
		{`{"data":[{"index":0,"embedding":[]}]}`, "embeddings"},
		{`{"data":[{"index":1,"embedding":[1]}]}`, "embeddings"},
		{`{"data":[{"embedding":[1]}]}`, "embeddings"},
		{`{"error":{"message":"failed"},"data":[{"index":0,"embedding":[1]}]}`, "embeddings"},
		{`{"results":[]}`, "rerank"},
		{`{"results":[{"index":1,"relevance_score":0.5}]}`, "rerank"},
		{`{"results":[{"index":0}]}`, "rerank"},
		{`{"error":{"message":"failed"},"results":[{"index":0,"relevance_score":0.5}]}`, "rerank"},
	}
	for i, test := range tests {
		t.Run(fmt.Sprint(i), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, test.body) }))
			defer server.Close()
			driver := NewSiliconFlowModel(map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings", Rerank: "rerank"})
			key, name := "key", "model"
			config := &APIConfig{APIKey: &key}
			var err error
			if test.path == "embeddings" {
				_, err = driver.Encode(&name, []string{"q"}, config, nil)
			} else {
				_, err = driver.Rerank(&name, "q", []string{"doc"}, config)
			}
			if err == nil {
				t.Fatal("malformed response accepted")
			}
		})
	}
}

func TestBoundChatPreservesHistoryAndGenerationConfig(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Messages  []map[string]string `json:"messages"`
			MaxTokens int                 `json:"max_tokens"`
			Thinking  map[string]string   `json:"thinking"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if !reflect.DeepEqual(body.Messages, []map[string]string{{"role": "system", "content": "system"}, {"role": "user", "content": "user"}, {"role": "assistant", "content": "assistant"}, {"role": "user", "content": "question"}}) || body.MaxTokens != 42 || body.Thinking["type"] != "disabled" {
			t.Errorf("body=%#v", body)
		}
		fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
	}))
	defer server.Close()
	driver := NewVolcEngine(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
	key, name, thinking := "key", "doubao", true
	model := NewChatModel(driver, &name, &APIConfig{APIKey: &key})
	model.ModelConfig.Thinking = &thinking
	answer, err := model.Chat("system", []map[string]string{{"role": "user", "content": "user"}, {"role": "assistant", "content": "assistant"}, {"role": "user", "content": "question"}}, map[string]interface{}{"max_tokens": float64(42), "thinking": false})
	if err != nil || answer != "answer" {
		t.Fatalf("answer=%q error=%v", answer, err)
	}
}

func TestSiliconFlowRejectsDuplicateIndexesAndDimensions(t *testing.T) {
	for _, test := range []struct{ path, body string }{
		{"embeddings", `{"data":[{"index":0,"embedding":[1,2]},{"index":0,"embedding":[3,4]}]}`},
		{"embeddings", `{"data":[{"index":0,"embedding":[1,2]},{"index":1,"embedding":[3]}]}`},
		{"rerank", `{"results":[{"index":0,"relevance_score":0.5},{"index":0,"relevance_score":0.8}]}`},
	} {
		t.Run(test.path+test.body, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, test.body) }))
			defer server.Close()
			driver := NewSiliconFlowModel(map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings", Rerank: "rerank"})
			name, key := "model", "key"
			config := &APIConfig{APIKey: &key}
			var err error
			if test.path == "embeddings" {
				_, err = driver.Encode(&name, []string{"a", "b"}, config, nil)
			} else {
				_, err = driver.Rerank(&name, "q", []string{"a", "b"}, config)
			}
			if err == nil {
				t.Fatal("ambiguous provider results accepted")
			}
		})
	}
}
