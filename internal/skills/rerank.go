package skills

import (
	"bytes"
	"context"
	"encoding/json"
	"math"
	"net/http"
	"strings"
	"time"
)

// strictRerank avoids drivers that return zero vectors on malformed responses.
func strictRerank(ctx context.Context, base, key, model, query string, texts []string) ([]float64, error) {
	body, e := json.Marshal(map[string]any{"model": model, "query": query, "documents": texts})
	if e != nil {
		return nil, e
	}
	url := strings.TrimSuffix(base, "/")
	if !strings.HasSuffix(url, "/rerank") {
		url += "/rerank"
	}
	req, e := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if e != nil {
		return nil, fault(503, "RERANK_FAILED")
	}
	req.Header.Set("Content-Type", "application/json")
	if key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	client := http.Client{Timeout: 60 * time.Second}
	response, e := client.Do(req)
	if e != nil {
		return nil, fault(503, "RERANK_FAILED")
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, fault(503, "RERANK_FAILED")
	}
	raw, e := readLimit(response.Body, 4*1024*1024)
	if e != nil {
		return nil, fault(503, "RERANK_FAILED")
	}
	var data struct {
		Results []struct {
			Index *int     `json:"index"`
			Score *float64 `json:"relevance_score"`
		} `json:"results"`
		Data []struct {
			Index *int     `json:"index"`
			Score *float64 `json:"relevance_score"`
		} `json:"data"`
	}
	if json.Unmarshal(raw, &data) != nil {
		return nil, fault(503, "RERANK_FAILED")
	}
	items := data.Results
	if items == nil {
		items = data.Data
	}
	if len(items) != len(texts) {
		return nil, fault(503, "RERANK_FAILED")
	}
	out := make([]float64, len(texts))
	seen := map[int]bool{}
	for _, item := range items {
		if item.Index == nil || item.Score == nil || *item.Index < 0 || *item.Index >= len(texts) || seen[*item.Index] || math.IsNaN(*item.Score) || math.IsInf(*item.Score, 0) {
			return nil, fault(503, "RERANK_FAILED")
		}
		seen[*item.Index] = true
		out[*item.Index] = *item.Score
	}
	return out, nil
}
