package models

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"strings"
)

// OpenRouterModel uses the shared chat transport and OpenRouter reasoning fields.
type OpenRouterModel struct{ *openAIChatDriver }

func NewOpenRouterModel(baseURL map[string]string, suffix URLSuffix) *OpenRouterModel {
	driver := newOpenAIChatDriver(baseURL, suffix, "openrouter", openRouterChatBody)
	driver.openRouterReasoning = true
	return &OpenRouterModel{driver}
}

func openRouterChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	body, err := openAIChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	history := make([]map[string]any, len(messages))
	for i, message := range messages {
		history[i] = map[string]any{"role": message.Role, "content": message.Content}
		if message.ReasoningContent != nil {
			history[i]["reasoning"] = *message.ReasoningContent
		}
	}
	body["messages"] = history
	if config != nil {
		if config.Thinking != nil {
			body["reasoning"] = map[string]any{"enabled": *config.Thinking}
		}
		if config.Effort != nil && *config.Effort != "" && *config.Effort != "default" && (config.Thinking == nil || *config.Thinking) {
			body["reasoning"] = map[string]any{"effort": *config.Effort}
		}
	}
	return body, nil
}

func (o *OpenRouterModel) Encode(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([][]float64, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	return encodeHTTP(o.httpClient, o.BaseURL, o.URLSuffix.Embedding, name, texts, local)
}

func (o *OpenRouterModel) Rerank(name *string, query string, texts []string, api *APIConfig) ([]float64, error) {
	if len(texts) == 0 {
		return []float64{}, nil
	}
	if name == nil || strings.TrimSpace(*name) == "" {
		return nil, fmt.Errorf("openrouter: rerank model name is required")
	}
	body := map[string]any{"model": *name, "query": query, "documents": texts, "top_n": len(texts)}
	local, cancel := modelCallConfig(api)
	defer cancel()
	response, err := o.request(http.MethodPost, o.URLSuffix.Rerank, local, body)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	var result struct {
		Results []struct {
			Index *int     `json:"index"`
			Score *float64 `json:"relevance_score"`
		} `json:"results"`
		Error json.RawMessage `json:"error"`
	}
	if err := json.NewDecoder(response.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("openrouter: decode rerank: %w", err)
	}
	if len(result.Error) > 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("openrouter: rerank provider error")
	}
	if len(result.Results) != len(texts) {
		return nil, fmt.Errorf("openrouter: rerank result count mismatch")
	}
	scores := make([]float64, len(texts))
	seen := make([]bool, len(texts))
	for _, item := range result.Results {
		if item.Index == nil || *item.Index < 0 || *item.Index >= len(texts) || seen[*item.Index] || item.Score == nil || math.IsNaN(*item.Score) || math.IsInf(*item.Score, 0) {
			return nil, fmt.Errorf("openrouter: missing, duplicate or invalid rerank index/score")
		}
		seen[*item.Index] = true
		scores[*item.Index] = *item.Score
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return scores, nil
}

func (o *OpenRouterModel) Balance(api *APIConfig) (map[string]interface{}, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	response, err := o.request(http.MethodGet, o.URLSuffix.Balance, local, nil)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	var result struct {
		Data *struct {
			Credits *float64 `json:"total_credits"`
			Usage   *float64 `json:"total_usage"`
		} `json:"data"`
		Error json.RawMessage `json:"error"`
	}
	if err := json.NewDecoder(response.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("openrouter: decode credits: %w", err)
	}
	if len(result.Error) > 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("openrouter: credits provider error")
	}
	if result.Data == nil || result.Data.Credits == nil || result.Data.Usage == nil {
		return nil, fmt.Errorf("openrouter: credits response is missing required values")
	}
	credits, usage := *result.Data.Credits, *result.Data.Usage
	balance := credits - usage
	if math.IsNaN(credits) || math.IsInf(credits, 0) || math.IsNaN(usage) || math.IsInf(usage, 0) || math.IsNaN(balance) || math.IsInf(balance, 0) {
		return nil, fmt.Errorf("openrouter: invalid credits values")
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return map[string]interface{}{"total_credits": credits, "total_usage": usage, "balance": balance, "currency": "USD"}, nil
}

func (o *OpenRouterModel) CheckConnection(api *APIConfig) error {
	_, err := o.Balance(api)
	return err
}

var _ ModelDriver = (*OpenRouterModel)(nil)
