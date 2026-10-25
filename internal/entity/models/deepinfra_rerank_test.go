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
)

func TestDeepInfraNativeRerank(t *testing.T) {
	var calls atomic.Int32
	server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Method != "POST" || r.URL.Path != "/v1/inference/Qwen/Qwen3-Reranker-4B" || r.Header.Get("Authorization") != "Bearer key" {
			t.Error("request shape", r.URL, r.Header)
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body["query"] != "query" || len(body) != 2 || !reflect.DeepEqual(body["documents"], []any{"a", "b", "c"}) {
			t.Error(body)
		}
		fmt.Fprint(w, `{"scores":[0,0.9,0.9]}`)
	}))
	defer server.Close()
	driver := NewDeepInfraModel(map[string]string{"default": server.URL}, URLSuffix{Rerank: "v1/inference"})
	key, name := "key", "Qwen/Qwen3-Reranker-4B"
	api := &APIConfig{APIKey: &key}
	for _, top := range []int{0, 1, 2, 99} {
		result, err := driver.Rerank(&name, "query", []string{"a", "b", "c"}, api, &RerankConfig{TopN: top})
		want := []RerankResult{{Index: 1, RelevanceScore: 0.9}, {Index: 2, RelevanceScore: 0.9}, {Index: 0, RelevanceScore: 0}}
		if top > 0 && top < 3 {
			want = want[:top]
		}
		if err != nil || !reflect.DeepEqual(result.Data, want) {
			t.Fatal(result, err)
		}
	}
	scores, err := NewRerankModel(driver, &name, api).Rerank("query", []string{"a", "b", "c"})
	if err != nil || !reflect.DeepEqual(scores, []float64{0, 0.9, 0.9}) {
		t.Fatal("bound consumer index reconstruction", scores, err)
	}
	before := calls.Load()
	if result, err := driver.Rerank(nil, "", nil, nil, nil); err != nil || result.Data == nil {
		t.Fatal(result, err)
	}
	for _, bad := range []string{"", " ", "../x", "a/../x", "x?key=secret", "x#y", "x%2fy"} {
		if _, err := driver.Rerank(&bad, "q", []string{"a"}, api, nil); err == nil {
			t.Fatal("bad model accepted", bad)
		}
	}
	if _, err := driver.Rerank(&name, "q", []string{"a"}, api, &RerankConfig{TopN: -1}); err == nil {
		t.Fatal("negative top")
	}
	if _, err := driver.Rerank(&name, "q", []string{"a"}, nil, nil); err == nil {
		t.Fatal("missing key")
	}
	if calls.Load() != before {
		t.Fatal("preflight reached HTTP")
	}
}
func TestDeepInfraRerankRawAndCancellation(t *testing.T) {
	for _, body := range []string{`{}`, `{"scores":null}`, `{"scores":[]}`, `{"scores":[null]}`, `{"scores":[1,2]}`, `{"scores":[1e400]}`, `{"scores":["0"]}`, `{"scores":[0],"error":"private-secret"}`, `{"scores":[0]} {}`} {
		server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
		driver := NewDeepInfraModel(map[string]string{"default": server.URL}, URLSuffix{Rerank: "v1/inference"})
		key, name := "key", "ns/m"
		if result, err := driver.Rerank(&name, "q", []string{"a"}, &APIConfig{APIKey: &key}, nil); err == nil || result != nil || strings.Contains(err.Error(), "private-secret") {
			t.Fatal(result, err)
		}
		server.Close()
	}
	server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { <-r.Context().Done() }))
	defer server.Close()
	driver := NewDeepInfraModel(map[string]string{"default": server.URL}, URLSuffix{Rerank: "v1/inference"})
	key, name := "key", "ns/m"
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := driver.Rerank(&name, "q", []string{"a"}, &APIConfig{APIKey: &key, Context: ctx}, nil); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}
