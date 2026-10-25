package models

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"sort"
)

// Rerank uses the bge pair-list prediction schema. Scores are raw similarities,
// not probabilities. Every score retains its original document index.
func (r *ReplicateModel) Rerank(name *string, query string, documents []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	top, err := RerankTopN(len(documents), config)
	if err != nil {
		return nil, err
	}
	if len(documents) == 0 {
		return &RerankResponse{Data: []RerankResult{}}, nil
	}
	if name == nil {
		return nil, fmt.Errorf("replicate: rerank model required")
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	endpoint, version, err := r.predictionEndpoint(*name, local)
	if err != nil {
		return nil, err
	}
	pairs := make([][2]string, len(documents))
	for i, doc := range documents {
		pairs[i] = [2]string{query, doc}
	}
	encoded, err := json.Marshal(pairs)
	if err != nil {
		return nil, err
	}
	body := map[string]any{"input": map[string]any{"input_list": string(encoded)}, "stream": false}
	if version != "" {
		body["version"] = version
	}
	prediction, err := r.prediction(endpoint, http.MethodPost, local, body, true)
	if err != nil {
		return nil, err
	}
	prediction, err = r.wait(prediction, endpoint, local)
	if err != nil {
		return nil, err
	}
	raw := prediction.Output
	if len(raw) > 0 && raw[0] == '{' {
		var object struct {
			Scores json.RawMessage `json:"scores"`
		}
		if err := json.Unmarshal(raw, &object); err != nil {
			return nil, fmt.Errorf("replicate: invalid rerank output")
		}
		raw = object.Scores
	}
	var scores []*float64
	if err := json.Unmarshal(raw, &scores); err != nil || len(scores) != len(documents) {
		return nil, fmt.Errorf("replicate: invalid rerank score count or type")
	}
	results := make([]RerankResult, len(scores))
	for i, score := range scores {
		if score == nil || math.IsNaN(*score) || math.IsInf(*score, 0) {
			return nil, fmt.Errorf("replicate: missing or non-finite rerank score")
		}
		results[i] = RerankResult{Index: i, RelevanceScore: *score}
	}
	sort.SliceStable(results, func(i, j int) bool { return results[i].RelevanceScore > results[j].RelevanceScore })
	response := &RerankResponse{Data: results[:top]}
	if err := ValidateRerankResponse(response, len(documents), config); err != nil {
		return nil, err
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return response, nil
}
