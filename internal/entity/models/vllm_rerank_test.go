package models

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
)

func TestVLLMRerankStrictHTTPContract(t *testing.T) {
	name, key := "reranker", "controlled-vllm-key"
	for _, auth := range []bool{false, true} {
		for _, config := range []*RerankConfig{nil, {}, {TopN: 1}, {TopN: 99}} {
			t.Run(fmt.Sprintf("auth=%v/config=%v", auth, config), func(t *testing.T) {
				top, _ := RerankTopN(2, config)
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.Method != "POST" || r.URL.Path != "/v1/rerank" || (r.Header.Get("Authorization") != "") == !auth {
						t.Error("vLLM path/auth mismatch")
					}
					var body map[string]any
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
					}
					if body["model"] != name || body["query"] != "query" || body["top_n"] != float64(top) || !reflect.DeepEqual(body["documents"], []any{"one", "two"}) {
						t.Error("vLLM flat document/TopN contract lost", body)
					}
					items := []map[string]any{{"index": 1, "relevance_score": .8}}
					if top == 2 {
						items = append(items, map[string]any{"index": 0, "relevance_score": 0})
					}
					json.NewEncoder(w).Encode(map[string]any{"results": items})
				}))
				defer srv.Close()
				d := NewVLLMModel(map[string]string{"default": srv.URL + "/v1"}, URLSuffix{Rerank: "rerank"})
				api := &APIConfig{}
				if auth {
					api.APIKey = &key
				}
				r, err := d.Rerank(&name, "query", []string{"one", "two"}, api, config)
				if err != nil || len(r.Data) != top || r.Data[0].Index != 1 {
					t.Fatal(r, err)
				}
				if top == 2 && r.Data[1].RelevanceScore != 0 {
					t.Fatal("legal zero lost")
				}
			})
		}
	}
	for _, raw := range []string{`{}`, `{"results":[]}`, `{"results":[{"index":0,"relevance_score":1}]}`, `{"results":[{"index":0,"relevance_score":0},{"index":0,"relevance_score":1}]}`, `{"results":[{"index":-1,"relevance_score":0},{"index":1,"relevance_score":1}]}`, `{"results":[{"index":2,"relevance_score":0},{"index":1,"relevance_score":1}]}`, `{"results":[{"index":0,"relevance_score":null},{"index":1,"relevance_score":1}]}`, `{"results":[{"index":null,"relevance_score":0},{"index":1,"relevance_score":1}]}`, `{"results":[{"index":0},{"index":1,"relevance_score":1}]}`, `{"results":[{"index":0,"relevance_score":NaN}]}`, `{"results":[{"index":0,"relevance_score":1e999}]}`, `{"error":"private-body"}`} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, raw) }))
		d := NewVLLMModel(map[string]string{"default": srv.URL}, URLSuffix{Rerank: "rerank"})
		if _, err := d.Rerank(&name, "q", []string{"a", "b"}, nil, nil); err == nil || strings.Contains(err.Error(), "private-body") {
			t.Fatal("invalid result accepted/leaked", raw, err)
		}
		srv.Close()
	}
}

func TestVLLMRerankPreflightAndCancellation(t *testing.T) {
	name := "reranker"
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(502)
		fmt.Fprint(w, "private-body")
	}))
	defer srv.Close()
	d := NewVLLMModel(map[string]string{"default": srv.URL}, URLSuffix{Rerank: "rerank"})
	if r, err := d.Rerank(nil, "q", nil, nil, nil); err != nil || len(r.Data) != 0 {
		t.Fatal(r, err)
	}
	if _, err := d.Rerank(&name, "q", []string{"a"}, nil, &RerankConfig{TopN: -1}); err == nil {
		t.Fatal("negative TopN accepted")
	}
	if _, err := d.Rerank(nil, "q", []string{"a"}, nil, nil); err == nil {
		t.Fatal("missing model accepted")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := d.Rerank(&name, "q", []string{"a"}, &APIConfig{Context: ctx}, nil); err == nil {
		t.Fatal("cancellation lost")
	}
	if calls != 0 {
		t.Fatal("preflight called provider")
	}
	if _, err := d.Rerank(&name, "q", []string{"a"}, nil, nil); err == nil || strings.Contains(err.Error(), "private-body") {
		t.Fatal("HTTP error lost/leaked", err)
	}
}
