package models

import (
	"encoding/json"
	"net/http"

	"testing"
)

func TestStructuredRerankTopNHTTP(t *testing.T) {
	for _, provider := range []string{"Aliyun", "Gitee", "ZHIPU-AI", "OpenRouter", "SiliconFlow"} {
		t.Run(provider, func(t *testing.T) {
			for _, config := range []*RerankConfig{nil, {}, {TopN: 1}, {TopN: 2}, {TopN: 10}} {
				top, err := expectedRerankTopNForTest(3, config)
				if err != nil {
					t.Fatal(err)
				}
				requested := top
				if config != nil && config.TopN != 0 {
					requested = config.TopN
				}
				server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					var body map[string]any
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
					}
					if body["top_n"] != float64(requested) || r.URL.Path != "/rerank" || r.Header.Get("Authorization") != "Bearer fixture" {
						t.Error(body, r.URL)
					}
					rows := []RerankResult{{Index: 2, RelevanceScore: 0}}
					if top > 1 {
						rows = append(rows, RerankResult{Index: 0, RelevanceScore: .9})
					}
					if top > 2 {
						rows = append(rows, RerankResult{Index: 1, RelevanceScore: .5})
					}
					field := "results"
					if provider == "Gitee" {
						field = "data"
					}
					json.NewEncoder(w).Encode(map[string]any{field: rows})
				}))
				driver, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Rerank: "rerank"})
				key, name := "fixture", "m"
				response, err := driver.Rerank(&name, "q", []string{"a", "b", "c"}, &APIConfig{ApiKey: &key}, config)
				server.Close()
				if err != nil || len(response.Data) != top {
					t.Fatal(response, err)
				}
				if response.Data[0].Index != 2 || response.Data[0].RelevanceScore != 0 {
					t.Fatal("zero or index lost", response)
				}
				if top > 1 && response.Data[1].Index != 0 {
					t.Fatal("not ranked", response)
				}
			}
		})
	}
}
func TestStructuredRerankValidation(t *testing.T) {
	for _, raw := range []string{`null`, `{}`, `{"index":null,"relevance_score":0}`, `{"index":-1,"relevance_score":0}`, `{"index":0}`, `{"index":0,"relevance_score":null}`, `{"index":0,"relevance_score":1e999}`} {
		var result RerankResult
		if err := json.Unmarshal([]byte(raw), &result); err == nil {
			t.Fatal("invalid result accepted", raw)
		}
	}
	var result RerankResult
	if err := json.Unmarshal([]byte(`{"index":0,"relevance_score":0}`), &result); err != nil || result.Index != 0 || result.RelevanceScore != 0 {
		t.Fatal("explicit zero lost", result, err)
	}
}
