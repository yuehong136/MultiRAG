package models

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/url"
	"sort"
	"strings"
)

func (d *DeepInfraModel) Rerank(name *string, query string, texts []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	top, err := RerankTopN(len(texts), config)
	if err != nil {
		return nil, err
	}
	if len(texts) == 0 {
		return &RerankResponse{Data: []RerankResult{}}, nil
	}
	if name == nil || strings.TrimSpace(*name) == "" || strings.TrimSpace(*name) != *name {
		return nil, fmt.Errorf("deepinfra: rerank model required")
	}
	// Keep the model namespace, while excluding URL control characters and traversal.
	for _, segment := range strings.Split(*name, "/") {
		if segment == "" || segment == "." || segment == ".." || strings.ContainsAny(segment, "?#%\\\r\n") {
			return nil, fmt.Errorf("deepinfra: invalid rerank model path")
		}
	}
	if d.URLSuffix.Rerank == "" {
		return nil, fmt.Errorf("deepinfra: rerank endpoint is not configured")
	}
	segments := strings.Split(*name, "/")
	for i := range segments {
		segments[i] = url.PathEscape(segments[i])
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	client := *d.httpClient
	client.CheckRedirect = rejectModelRedirect
	driver := *d.openAIChatDriver
	driver.httpClient = &client
	resp, err := driver.request(http.MethodPost, strings.TrimRight(d.URLSuffix.Rerank, "/")+"/"+strings.Join(segments, "/"), local, map[string]any{"query": query, "documents": texts})
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var raw struct {
		Scores []*float64      `json:"scores"`
		Error  json.RawMessage `json:"error"`
	}
	if err := decodeBoundedModelJSON(resp.Body, &raw); err != nil {
		return nil, fmt.Errorf("deepinfra: invalid rerank response: %w", err)
	}
	if len(raw.Error) > 0 && string(raw.Error) != "null" {
		return nil, fmt.Errorf("deepinfra: rerank provider error")
	}
	if len(raw.Scores) != len(texts) {
		return nil, fmt.Errorf("deepinfra: expected %d scores, got %d", len(texts), len(raw.Scores))
	}
	result := &RerankResponse{Data: make([]RerankResult, len(texts))}
	for i, score := range raw.Scores {
		if score == nil || math.IsNaN(*score) || math.IsInf(*score, 0) {
			return nil, fmt.Errorf("deepinfra: missing or invalid rerank score")
		}
		result.Data[i] = RerankResult{Index: i, RelevanceScore: *score}
	}
	sort.SliceStable(result.Data, func(i, j int) bool { return result.Data[i].RelevanceScore > result.Data[j].RelevanceScore })
	result.Data = result.Data[:top]
	if err := ValidateRerankResponse(result, len(texts), config); err != nil {
		return nil, err
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return result, nil
}
