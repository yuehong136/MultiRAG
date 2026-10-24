package models

import (
	"fmt"
	"net/http"
	"strings"
)

type TogetherAIModel struct{ *openAIChatDriver }

func NewTogetherAIModel(urls map[string]string, suffix URLSuffix) *TogetherAIModel {
	d := newOpenAIChatDriver(urls, suffix, "togetherai", togetherAIChatBody)
	d.reasoningAlias, d.rootFinishReason = true, true
	return &TogetherAIModel{d}
}

func togetherAIChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	if stream && config != nil && config.Stream != nil && !*config.Stream {
		return nil, fmt.Errorf("togetherai: stream must be true")
	}
	body, err := openAIChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	if config != nil {
		if config.Thinking != nil {
			body["reasoning"] = map[string]any{"enabled": *config.Thinking}
		}
		if config.Effort != nil && strings.Contains(strings.ToLower(name), "gpt-oss") {
			body["reasoning_effort"] = *config.Effort
		}
	}
	return body, nil
}

func (t *TogetherAIModel) ListModels(api *APIConfig) ([]string, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := t.request(http.MethodGet, t.URLSuffix.Models, local, nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result *[]struct {
		ID string `json:"id"`
	}
	if err := decodeBoundedModelJSON(resp.Body, &result); err != nil || result == nil {
		return nil, fmt.Errorf("togetherai: invalid model list")
	}
	names, seen := make([]string, 0, len(*result)), map[string]bool{}
	for _, item := range *result {
		if strings.TrimSpace(item.ID) == "" || seen[item.ID] {
			return nil, fmt.Errorf("togetherai: invalid or duplicate model ID")
		}
		seen[item.ID] = true
		names = append(names, item.ID)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return names, nil
}

func (t *TogetherAIModel) CheckConnection(api *APIConfig) error {
	_, err := t.ListModels(api)
	return err
}

func (t *TogetherAIModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(t.httpClient, t.BaseURL, t.URLSuffix.Embedding, name, texts, api, config)
}

var _ ModelDriver = (*TogetherAIModel)(nil)
