package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestZhipuRerankHTTPOrderAndPayload(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Method != "POST" || r.URL.Path != "/instance/v4/rerank" || r.Header.Get("Content-Type") != "application/json" || r.Header.Get("Authorization") != "Bearer fixture" {
			t.Error("rerank URL/auth/request contract")
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		want := map[string]any{"model": "glm-rerank", "query": "query", "documents": []any{"zero", "one", "two"}, "top_n": float64(3), "return_documents": false}
		if !reflect.DeepEqual(body, want) {
			t.Fatalf("body=%v want=%v", body, want)
		}
		fmt.Fprint(w, `{"results":[{"index":2,"relevance_score":0.7},{"index":0,"relevance_score":0},{"index":1,"relevance_score":0.2}]}`)
	}))
	defer server.Close()
	driver, err := NewModelFactory().CreateModelDriver("ZHIPU-AI", map[string]string{"default": "http://catalog.invalid", "office": server.URL + "/instance/v4/"}, URLSuffix{Rerank: "/rerank"})
	if err != nil {
		t.Fatal(err)
	}
	key, name, region := "fixture", "glm-rerank", "office"
	api := &APIConfig{APIKey: &key, Region: &region}
	bound := NewRerankModel(driver, &name, api)
	scores, err := bound.Rerank("query", []string{"zero", "one", "two"})
	if err != nil || !reflect.DeepEqual(scores, []float64{0, 0.2, 0.7}) || calls.Load() != 1 {
		t.Fatal(scores, err, calls.Load())
	}
	if api.Context != nil {
		t.Fatal("request config mutated")
	}
	t.Log("POST /instance/v4/rerank: exact payload and ordered scores [0, 0.2, 0.7] verified; explicit zero retained")
}

func TestZhipuRerankInvalidResponsesHTTP(t *testing.T) {
	for _, test := range []struct {
		name, body string
		status     int
	}{
		{"empty body", "", 200}, {"malformed", "not-json", 200}, {"null body", "null", 200}, {"absent results", `{}`, 200}, {"null results", `{"results":null}`, 200}, {"wrong results", `{"results":{}}`, 200}, {"missing result", `{"results":[{"index":0,"relevance_score":0.1}]}`, 200}, {"extra result", `{"results":[{"index":0,"relevance_score":0},{"index":1,"relevance_score":0},{"index":2,"relevance_score":0}]}`, 200},
		{"missing index", `{"results":[{"relevance_score":0.1},{"index":1,"relevance_score":0.2}]}`, 200}, {"null index", `{"results":[{"index":null,"relevance_score":0.1},{"index":1,"relevance_score":0.2}]}`, 200}, {"duplicate index", `{"results":[{"index":0,"relevance_score":0.1},{"index":0,"relevance_score":0.2}]}`, 200}, {"negative index", `{"results":[{"index":-1,"relevance_score":0.1},{"index":1,"relevance_score":0.2}]}`, 200}, {"out of range", `{"results":[{"index":2,"relevance_score":0.1},{"index":1,"relevance_score":0.2}]}`, 200}, {"fractional index", `{"results":[{"index":0.5,"relevance_score":0.1},{"index":1,"relevance_score":0.2}]}`, 200}, {"string index", `{"results":[{"index":"0","relevance_score":0.1},{"index":1,"relevance_score":0.2}]}`, 200},
		{"missing score", `{"results":[{"index":0},{"index":1,"relevance_score":0.2}]}`, 200}, {"null score", `{"results":[{"index":0,"relevance_score":null},{"index":1,"relevance_score":0.2}]}`, 200}, {"string score", `{"results":[{"index":0,"relevance_score":"0.1"},{"index":1,"relevance_score":0.2}]}`, 200}, {"overflow score", `{"results":[{"index":0,"relevance_score":1e999},{"index":1,"relevance_score":0.2}]}`, 200}, {"NaN score", `{"results":[{"index":0,"relevance_score":NaN},{"index":1,"relevance_score":0.2}]}`, 200}, {"infinite score", `{"results":[{"index":0,"relevance_score":Infinity},{"index":1,"relevance_score":0.2}]}`, 200},
		{"provider error", `{"error":{"code":"private-body"},"results":[{"index":0,"relevance_score":0.1},{"index":1,"relevance_score":0.2}]}`, 200}, {"HTTP error", "private-body", 503},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(test.status); fmt.Fprint(w, test.body) }))
			defer server.Close()
			driver := NewZhipuAIModel(map[string]string{"default": server.URL}, URLSuffix{Rerank: "rerank"})
			key, name := "fixture", "glm-rerank"
			scores, err := driver.Rerank(&name, "q", []string{"one", "two"}, &APIConfig{APIKey: &key})
			if err == nil || scores != nil || strings.Contains(err.Error(), "private-body") {
				t.Fatal("invalid response yielded scores or exposed provider body", scores, err)
			}
		})
	}
}

func TestZhipuRerankEmptyInputAndPreflight(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); fmt.Fprint(w, `{}`) }))
	defer server.Close()
	driver := NewZhipuAIModel(map[string]string{"default": server.URL}, URLSuffix{Rerank: "rerank"})
	key, name, unknown := "fixture", "glm-rerank", "missing"
	if scores, err := driver.Rerank(nil, "", nil, nil); err != nil || scores == nil || len(scores) != 0 || calls.Load() != 0 {
		t.Fatal(scores, err, calls.Load())
	}
	for _, test := range []struct {
		model *string
		api   *APIConfig
	}{
		{nil, &APIConfig{APIKey: &key}}, {stringPtr(" "), &APIConfig{APIKey: &key}}, {&name, nil}, {&name, &APIConfig{}}, {&name, &APIConfig{APIKey: stringPtr(" ")}}, {&name, &APIConfig{APIKey: &key, Region: &unknown}},
	} {
		if scores, err := driver.Rerank(test.model, "q", []string{"d"}, test.api); err == nil || scores != nil {
			t.Fatal(scores, err)
		}
	}
	for _, d := range []*ZhipuAIModel{NewZhipuAIModel(nil, URLSuffix{Rerank: "rerank"}), NewZhipuAIModel(map[string]string{"default": server.URL}, URLSuffix{})} {
		if _, err := d.Rerank(&name, "q", []string{"d"}, &APIConfig{APIKey: &key}); err == nil {
			t.Fatal("missing URL/suffix accepted")
		}
	}
	if calls.Load() != 0 {
		t.Fatal("invalid request reached HTTP")
	}
}

func TestZhipuRerankCancelHTTP(t *testing.T) {
	for _, early := range []bool{false, true} {
		t.Run(fmt.Sprint(early), func(t *testing.T) {
			var calls atomic.Int32
			started, stopped := make(chan struct{}), make(chan struct{})
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				_, _ = io.Copy(io.Discard, r.Body)
				r.Body.Close()
				w.WriteHeader(200)
				w.(http.Flusher).Flush()
				close(started)
				select {
				case <-r.Context().Done():
				case <-time.After(2 * time.Second):
					t.Error("provider request did not cancel")
				}
				close(stopped)
			}))
			defer server.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			if early {
				cancel()
			} else {
				go func() { <-started; cancel() }()
			}
			driver := NewZhipuAIModel(map[string]string{"default": server.URL}, URLSuffix{Rerank: "rerank"})
			key, name := "fixture", "glm-rerank"
			scores, err := driver.Rerank(&name, "q", []string{"d"}, &APIConfig{APIKey: &key, Context: ctx})
			if !errors.Is(err, context.Canceled) || scores != nil {
				t.Fatal(scores, err)
			}
			if early {
				if calls.Load() != 0 {
					t.Fatal("canceled request reached HTTP")
				}
			} else {
				select {
				case <-stopped:
				case <-time.After(time.Second):
					t.Fatal("HTTP cleanup failed")
				}
			}
		})
	}
}
