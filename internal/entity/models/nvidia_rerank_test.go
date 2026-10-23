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

func TestNVIDIARerankPayloadAndTopNHTTP(t *testing.T) {
	for _, config := range []*RerankConfig{nil, {}, {TopN: 1}, {TopN: 99}} {
		t.Run(fmt.Sprint(config), func(t *testing.T) {
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				top := 3
				if config != nil && config.TopN == 1 {
					top = 1
				}
				if r.Method != http.MethodPost || r.URL.Path != "/office/v1/ranking" || r.Header.Get("Authorization") != "Bearer fixture" || r.Header.Get("Content-Type") != "application/json" || body["model"] != "nvidia/reranker" || body["truncate"] != "END" || body["top_n"] != float64(top) || !reflect.DeepEqual(body["query"], map[string]any{"text": "q"}) || !reflect.DeepEqual(body["passages"], []any{map[string]any{"text": "zero"}, map[string]any{"text": "one"}, map[string]any{"text": "two"}}) {
					t.Error("incorrect NIM request", body, r.URL.Path)
				}
				if _, ok := body["documents"]; ok {
					t.Error("generic documents leaked")
				}
				if _, ok := body["return_documents"]; ok {
					t.Error("generic return_documents leaked")
				}
				if top == 1 {
					fmt.Fprint(w, `{"rankings":[{"index":2,"logit":2}]}`)
				} else {
					fmt.Fprint(w, `{"rankings":[{"index":1,"logit":-3},{"index":2,"logit":2},{"index":0,"logit":0}]}`)
				}
			}))
			defer server.Close()
			urls := map[string]string{"default": "http://unused.invalid", "office": server.URL + "/office/v1/"}
			driver := NewNVIDIAModel(urls, URLSuffix{Rerank: "ranking"})
			urls["office"] = "http://mutated.invalid"
			api := &APIConfig{APIKey: stringPtr(" fixture "), Region: stringPtr("office")}
			response, err := driver.Rerank(stringPtr("nvidia/reranker"), "q", []string{"zero", "one", "two"}, api, config)
			if err != nil {
				t.Fatal(err)
			}
			want := []RerankResult{{Index: 2, RelevanceScore: 2}}
			if config == nil || config.TopN != 1 {
				want = append(want, RerankResult{Index: 0, RelevanceScore: 0}, RerankResult{Index: 1, RelevanceScore: -3})
			}
			if !reflect.DeepEqual(response.Data, want) || calls.Load() != 1 || api.Context != nil || *api.APIKey != " fixture " {
				t.Fatal(response, calls.Load())
			}
			if config == nil || config.TopN != 1 {
				scores, err := NewRerankModel(driver, stringPtr("nvidia/reranker"), api).Rerank("q", []string{"zero", "one", "two"})
				if err != nil || !reflect.DeepEqual(scores, []float64{0, -3, 2}) {
					t.Fatal(scores, err)
				}
			}
		})
	}
}

func TestNVIDIARerankRejectsMalformedHTTP(t *testing.T) {
	for _, body := range []string{
		``, `null`, `{}`, `{"rankings":null}`, `{"rankings":[]}`,
		`{"rankings":[{"index":0,"logit":0}]}`,
		`{"rankings":[{"index":0,"logit":0},{"index":0,"logit":1}]}`,
		`{"rankings":[{"index":-1,"logit":0},{"index":1,"logit":1}]}`,
		`{"rankings":[{"index":2,"logit":0},{"index":1,"logit":1}]}`,
		`{"rankings":[{"logit":0},{"index":1,"logit":1}]}`,
		`{"rankings":[{"index":null,"logit":0},{"index":1,"logit":1}]}`,
		`{"rankings":[{"index":0},{"index":1,"logit":1}]}`,
		`{"rankings":[{"index":0,"logit":null},{"index":1,"logit":1}]}`,
		`{"rankings":[{"index":0.5,"logit":0},{"index":1,"logit":1}]}`,
		`{"rankings":[{"index":"0","logit":0},{"index":1,"logit":1}]}`,
		`{"rankings":[{"index":0,"logit":"0"},{"index":1,"logit":1}]}`,
		`{"rankings":[{"index":0,"logit":1e999},{"index":1,"logit":1}]}`,
		`{"rankings":[{"index":0,"logit":NaN},{"index":1,"logit":1}]}`,
		`{"rankings":[{"index":0,"logit":0},{"index":1,"logit":1}],"error":"private-body"}`,
		`{"rankings":[{"index":0,"logit":0},{"index":1,"logit":1}],"code":"private-body"}`,
		`{"rankings":[{"index":0,"logit":0},{"index":1,"logit":1}]} {}`,
		`{"results":[{"index":0,"relevance_score":0},{"index":1,"relevance_score":1}]}`,
	} {
		t.Run(body, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
			defer server.Close()
			driver := NewNVIDIAModel(map[string]string{"default": server.URL}, URLSuffix{Rerank: "ranking"})
			response, err := driver.Rerank(stringPtr("m"), "q", []string{"a", "b"}, &APIConfig{APIKey: stringPtr("fixture")}, nil)
			if err == nil || response != nil || strings.Contains(err.Error(), "private-body") {
				t.Fatal(response, err)
			}
		})
	}
}

func TestNVIDIARerankPreflightDeadlineAndFailure(t *testing.T) {
	driver := NewNVIDIAModel(map[string]string{"default": "http://fixture.invalid"}, URLSuffix{Rerank: "ranking"})
	calls := 0
	driver.httpClient.Transport = lmStudioTransportFunc(func(r *http.Request) (*http.Response, error) {
		calls++
		deadline, ok := r.Context().Deadline()
		if !ok || time.Until(deadline) <= 0 || time.Until(deadline) > 30*time.Second {
			t.Error("missing bounded deadline")
		}
		return &http.Response{StatusCode: 429, Body: io.NopCloser(strings.NewReader("private-body")), Header: make(http.Header)}, nil
	})
	response, err := driver.Rerank(nil, "", nil, nil, nil)
	if err != nil || response == nil || response.Data == nil || len(response.Data) != 0 {
		t.Fatal(response, err)
	}
	if response, err = driver.Rerank(nil, "", nil, nil, &RerankConfig{TopN: -1}); err == nil || response != nil {
		t.Fatal("negative TopN accepted")
	}
	api := &APIConfig{APIKey: stringPtr("fixture")}
	for _, name := range []*string{nil, stringPtr(""), stringPtr(" ")} {
		if response, err = driver.Rerank(name, "q", []string{"a"}, api, nil); err == nil || response != nil {
			t.Fatal("missing name accepted")
		}
	}
	for _, invalid := range []*APIConfig{nil, {}, {APIKey: stringPtr("")}, {APIKey: stringPtr(" ")}, {APIKey: stringPtr("fixture"), Region: stringPtr("unknown")}} {
		if response, err = driver.Rerank(stringPtr("m"), "q", []string{"a"}, invalid, nil); err == nil || response != nil {
			t.Fatal("invalid config accepted")
		}
	}
	if calls != 0 {
		t.Fatal("invalid request reached transport")
	}
	if response, err = driver.Rerank(stringPtr("m"), "q", []string{"a"}, api, nil); err == nil || response != nil || !strings.Contains(err.Error(), "429") || strings.Contains(err.Error(), "private-body") || calls != 1 {
		t.Fatal(response, err, calls)
	}
	for _, missing := range []*NVIDIAModel{NewNVIDIAModel(nil, URLSuffix{Rerank: "ranking"}), NewNVIDIAModel(driver.BaseURL, URLSuffix{})} {
		if response, err = missing.Rerank(stringPtr("m"), "q", []string{"a"}, api, nil); err == nil || response != nil {
			t.Fatal("missing endpoint accepted")
		}
	}
}

func TestNVIDIARerankCancellationHTTP(t *testing.T) {
	for _, early := range []bool{true, false} {
		t.Run(fmt.Sprint(early), func(t *testing.T) {
			var calls atomic.Int32
			started, stopped := make(chan struct{}), make(chan struct{})
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				io.Copy(io.Discard, r.Body)
				r.Body.Close()
				w.WriteHeader(200)
				w.(http.Flusher).Flush()
				close(started)
				select {
				case <-r.Context().Done():
				case <-time.After(2 * time.Second):
					t.Error("request did not cancel")
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
			driver := NewNVIDIAModel(map[string]string{"default": server.URL}, URLSuffix{Rerank: "ranking"})
			response, err := driver.Rerank(stringPtr("m"), "q", []string{"a"}, &APIConfig{APIKey: stringPtr("fixture"), Context: ctx}, nil)
			if !errors.Is(err, context.Canceled) || response != nil {
				t.Fatal(response, err)
			}
			if early {
				if calls.Load() != 0 {
					t.Fatal("cancelled request reached HTTP")
				}
			} else {
				select {
				case <-stopped:
				case <-time.After(time.Second):
					t.Fatal("request cleanup failed")
				}
			}
		})
	}
}

func TestNVIDIARerankHostedEndpoints(t *testing.T) {
	for name, endpoint := range map[string]string{
		"nvidia/nv-rerankqa-mistral-4b-v3":   "https://ai.api.nvidia.com/v1/retrieval/nvidia/reranking",
		"nvidia/llama-3.2-nv-rerankqa-1b-v2": "https://ai.api.nvidia.com/v1/retrieval/nvidia/llama-3_2-nv-rerankqa-1b-v2/reranking",
	} {
		t.Run(name, func(t *testing.T) {
			driver := NewNVIDIAModel(map[string]string{"default": "https://integrate.api.nvidia.com/v1/"}, URLSuffix{Rerank: "ranking"})
			calls := 0
			driver.httpClient.Transport = lmStudioTransportFunc(func(r *http.Request) (*http.Response, error) {
				calls++
				if r.URL.String() != endpoint {
					t.Error("wrong hosted endpoint", r.URL)
				}
				return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{"rankings":[{"index":0,"logit":0}]}`)), Header: make(http.Header)}, nil
			})
			api := &APIConfig{APIKey: stringPtr("fixture"), Region: stringPtr("")}
			response, err := driver.Rerank(&name, "q", []string{"a"}, api, nil)
			if err != nil || response == nil || calls != 1 || *api.Region != "" || api.Context != nil || driver.BaseURL["default"] != "https://integrate.api.nvidia.com/v1/" {
				t.Fatal(response, err, calls)
			}
		})
	}
}
