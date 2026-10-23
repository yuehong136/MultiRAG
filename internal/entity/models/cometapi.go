// Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
// Licensed under the Apache License, Version 2.0.
package models

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/url"
	"strings"
)

type CometAPIModel struct{ *openAIChatDriver }

func NewCometAPIModel(urls map[string]string, suffix URLSuffix) *CometAPIModel {
	d := newOpenAIChatDriver(urls, suffix, "cometapi", cometAPIChatBody)
	d.optionalDiscoveryKey = true
	d.waitForStreamEOF = true
	return &CometAPIModel{d}
}

func cometAPIChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	if stream && config != nil && config.Stream != nil && !*config.Stream {
		return nil, fmt.Errorf("cometapi: stream must be true for streaming chat")
	}
	return openAIChatBody(name, messages, config, stream)
}

func (c *CometAPIModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(c.httpClient, c.BaseURL, c.URLSuffix.Embedding, name, texts, api, config)
}

func (c *CometAPIModel) ListModels(api *APIConfig) ([]string, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	// Comet's public catalog does not require tenant credentials.
	local.APIKey = nil
	resp, err := c.request(http.MethodGet, c.URLSuffix.Models, local, nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result struct {
		Data *[]struct {
			ID string `json:"id"`
		} `json:"data"`
		Error json.RawMessage `json:"error"`
	}
	if err := decodeCoHereJSON(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("cometapi: invalid model list: %w", err)
	}
	if result.Data == nil || len(result.Error) > 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("cometapi: missing model list or provider error")
	}
	names := make([]string, 0, len(*result.Data))
	seen := map[string]bool{}
	for _, item := range *result.Data {
		if strings.TrimSpace(item.ID) == "" || seen[item.ID] {
			return nil, fmt.Errorf("cometapi: invalid or duplicate model ID")
		}
		seen[item.ID] = true
		names = append(names, item.ID)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return names, nil
}

func (c *CometAPIModel) Balance(api *APIConfig) (map[string]any, error) {
	if api == nil || api.APIKey == nil || strings.TrimSpace(*api.APIKey) == "" {
		return nil, fmt.Errorf("cometapi: API key is required")
	}
	endpoint := strings.TrimSpace(c.URLSuffix.Balance)
	if endpoint == "" {
		return nil, fmt.Errorf("cometapi: balance URL is required")
	}
	if !strings.HasPrefix(endpoint, "http://") && !strings.HasPrefix(endpoint, "https://") {
		endpoint = "https://query.cometapi.com/" + strings.TrimLeft(endpoint, "/")
	}
	parsed, err := url.Parse(endpoint)
	if err != nil || parsed.Host == "" || parsed.User != nil || parsed.Fragment != "" {
		return nil, fmt.Errorf("cometapi: invalid balance URL")
	}
	query := parsed.Query()
	query.Set("key", strings.TrimSpace(*api.APIKey))
	parsed.RawQuery = query.Encode()
	local, cancel := modelCallConfig(api)
	defer cancel()
	req, err := http.NewRequestWithContext(requestContext(local), http.MethodGet, parsed.String(), nil)
	if err != nil {
		return nil, fmt.Errorf("cometapi: invalid quota request")
	}
	// The quota API authenticates in its query. Never forward that request to a
	// redirect target or expose url.Error (which includes the secret query).
	client := *c.httpClient
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	resp, err := client.Do(req)
	if err != nil {
		if err := requestContext(local).Err(); err != nil {
			return nil, err
		}
		return nil, fmt.Errorf("cometapi: quota request failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("cometapi: quota request failed (HTTP %d)", resp.StatusCode)
	}
	var result map[string]any
	if err := decodeCoHereJSON(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("cometapi: invalid quota response")
	}
	if result["error"] != nil || result["success"] == false {
		return nil, fmt.Errorf("cometapi: quota provider error")
	}
	quota, ok := result["total_quota"].(float64)
	if !ok || quota < 0 || math.IsNaN(quota) || math.IsInf(quota, 0) {
		return nil, fmt.Errorf("cometapi: missing or invalid total_quota")
	}
	for _, field := range []string{"total_used_quota", "request_count"} {
		if value, exists := result[field]; exists {
			number, ok := value.(float64)
			if !ok || number < 0 || math.IsNaN(number) || math.IsInf(number, 0) || field == "request_count" && math.Trunc(number) != number {
				return nil, fmt.Errorf("cometapi: invalid quota usage")
			}
		}
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return result, nil
}

func (c *CometAPIModel) CheckConnection(api *APIConfig) error { _, err := c.Balance(api); return err }

var _ ModelDriver = (*CometAPIModel)(nil)
