package models

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"sort"
	"strings"
)

// RerankTopN resolves the all-documents default without mutating caller configuration.
func RerankTopN(count int, config *RerankConfig) (int, error) {
	if config == nil || config.TopN == 0 {
		return count, nil
	}
	if config.TopN < 0 {
		return 0, fmt.Errorf("top_n must be non-negative")
	}
	return min(count, config.TopN), nil
}

// ValidateRerankResponse rejects incomplete or ambiguous mappings, including valid zero scores.
func ValidateRerankResponse(response *RerankResponse, count int, config *RerankConfig) error {
	top, err := RerankTopN(count, config)
	if err != nil {
		return err
	}
	if response == nil || len(response.Data) != top {
		return fmt.Errorf("rerank result count mismatch")
	}
	seen := make([]bool, count)
	for _, result := range response.Data {
		if result.Index < 0 || result.Index >= count || seen[result.Index] || math.IsNaN(result.RelevanceScore) || math.IsInf(result.RelevanceScore, 0) {
			return fmt.Errorf("invalid or duplicate rerank index/score")
		}
		seen[result.Index] = true
	}
	return nil
}

func rerankHTTP(client *http.Client, urls map[string]string, suffix string, name *string, query string, texts []string, api *APIConfig, config *RerankConfig, extras map[string]any) (*RerankResponse, error) {
	top, err := RerankTopN(len(texts), config)
	if err != nil {
		return nil, err
	}
	if len(texts) == 0 {
		return &RerankResponse{Data: []RerankResult{}}, nil
	}
	if name == nil || strings.TrimSpace(*name) == "" || api == nil || api.APIKey == nil || strings.TrimSpace(*api.APIKey) == "" {
		return nil, fmt.Errorf("rerank model name and API key are required")
	}
	if suffix == "" {
		return nil, fmt.Errorf("rerank endpoint is not configured")
	}
	base, err := resolveModelBaseURL(urls, api.Region)
	if err != nil {
		return nil, err
	}
	body := map[string]any{"model": *name, "query": query, "documents": texts, "top_n": top, "return_documents": false}
	for key, value := range extras {
		if value == nil {
			delete(body, key)
		} else {
			body[key] = value
		}
	}
	data, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	req, err := http.NewRequestWithContext(requestContext(local), http.MethodPost, joinModelURL(base, suffix), bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*api.APIKey))
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("rerank request: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("rerank request failed (HTTP %d)", resp.StatusCode)
	}
	var raw struct {
		Results []struct {
			Index *int     `json:"index"`
			Score *float64 `json:"relevance_score"`
		} `json:"results"`
		Error json.RawMessage `json:"error"`
		Code  string          `json:"code"`
	}
	decoder := json.NewDecoder(resp.Body)
	if err := decoder.Decode(&raw); err != nil {
		return nil, fmt.Errorf("decode rerank: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return nil, fmt.Errorf("rerank response has trailing content")
	}
	if raw.Code != "" || len(raw.Error) > 0 && string(raw.Error) != "null" {
		return nil, fmt.Errorf("rerank provider returned an error")
	}
	response := &RerankResponse{Data: make([]RerankResult, 0, len(raw.Results))}
	for _, result := range raw.Results {
		if result.Index == nil || result.Score == nil {
			return nil, fmt.Errorf("rerank result missing index/score")
		}
		response.Data = append(response.Data, RerankResult{Index: *result.Index, RelevanceScore: *result.Score})
	}
	if err := ValidateRerankResponse(response, len(texts), config); err != nil {
		return nil, err
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	sort.SliceStable(response.Data, func(i, j int) bool { return response.Data[i].RelevanceScore > response.Data[j].RelevanceScore })
	return response, nil
}
