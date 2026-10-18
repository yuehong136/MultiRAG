package models

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
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

func (o *OpenRouterModel) Rerank(name *string, query string, texts []string, api *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return rerankHTTP(o.httpClient, o.BaseURL, o.URLSuffix.Rerank, name, query, texts, api, rerankConfig, map[string]any{"return_documents": nil})
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
