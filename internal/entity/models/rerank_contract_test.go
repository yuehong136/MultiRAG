package models

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

func TestStructuredRerankTopNHTTP(t *testing.T) {
	for _, provider := range []string{"Aliyun", "Gitee", "ZHIPU-AI", "OpenRouter", "SiliconFlow"} {
		t.Run(provider, func(t *testing.T) {
			for _, config := range []*RerankConfig{nil, {}, {TopN: 1}, {TopN: 2}, {TopN: 10}} {
				top, err := RerankTopN(3, config)
				if err != nil {
					t.Fatal(err)
				}
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					var body map[string]any
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
					}
					if body["top_n"] != float64(top) || r.URL.Path != "/rerank" || r.Header.Get("Authorization") != "Bearer fixture" {
						t.Error(body, r.URL)
					}
					if top == 1 {
						fmt.Fprint(w, `{"results":[{"index":2,"relevance_score":0}]}`)
					} else if top == 2 {
						fmt.Fprint(w, `{"results":[{"index":2,"relevance_score":0},{"index":0,"relevance_score":0.9}]}`)
					} else {
						fmt.Fprint(w, `{"results":[{"index":2,"relevance_score":0},{"index":0,"relevance_score":0.9},{"index":1,"relevance_score":0.5}]}`)
					}
				}))
				driver, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Rerank: "rerank"})
				key, name := "fixture", "m"
				response, err := driver.Rerank(&name, "q", []string{"a", "b", "c"}, &APIConfig{APIKey: &key}, config)
				server.Close()
				if err != nil || len(response.Data) != top {
					t.Fatal(response, err)
				}
				if response.Data[len(response.Data)-1].Index != 2 || response.Data[len(response.Data)-1].RelevanceScore != 0 {
					t.Fatal("zero or index lost", response)
				}
				if top > 1 && response.Data[0].Index != 0 {
					t.Fatal("not ranked", response)
				}
			}
		})
	}
}
func TestStructuredRerankValidation(t *testing.T) {
	for _, data := range [][]RerankResult{nil, {{Index: 0, RelevanceScore: 0}, {Index: 0, RelevanceScore: 1}}, {{Index: -1}, {Index: 1}}, {{Index: 2}, {Index: 1}}, {{Index: 0, RelevanceScore: math.Inf(1)}, {Index: 1}}, {{Index: 0, RelevanceScore: math.NaN()}, {Index: 1}}} {
		if err := ValidateRerankResponse(&RerankResponse{Data: data}, 2, nil); err == nil {
			t.Fatal(data)
		}
	}
	if err := ValidateRerankResponse(nil, 0, nil); err == nil {
		t.Fatal("nil success")
	}
	if err := ValidateRerankResponse(&RerankResponse{Data: []RerankResult{{Index: 0}, {Index: 1}}}, 2, nil); err != nil {
		t.Fatal(err)
	}
	if n, err := RerankTopN(2, &RerankConfig{TopN: -1}); err == nil || n != 0 {
		t.Fatal(n, err)
	}
	// Bound retrieval always reconstructs full input order even when provider order differs.
	response := &RerankResponse{Data: []RerankResult{{Index: 1, RelevanceScore: .7}, {Index: 0, RelevanceScore: 0}}}
	bound := NewRerankModel(&structuredRerankFixture{response: response}, nil, nil)
	scores, err := bound.Rerank("q", []string{"zero", "one"})
	if err != nil || !reflect.DeepEqual(scores, []float64{0, .7}) {
		t.Fatal(scores, err)
	}
	bound.ModelDriver = &structuredRerankFixture{}
	if _, err := bound.Rerank("q", []string{"d"}); err == nil {
		t.Fatal("nil result accepted")
	}
}

type structuredRerankFixture struct {
	ModelDriver
	response *RerankResponse
}

func (d *structuredRerankFixture) Rerank(_ *string, _ string, _ []string, _ *APIConfig, _ *RerankConfig) (*RerankResponse, error) {
	return d.response, nil
}
