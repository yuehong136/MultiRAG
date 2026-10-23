// Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
// Licensed under the Apache License, Version 2.0.
package models

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"strings"
)

type DeepInfraModel struct{ *openAIChatDriver }

func NewDeepInfraModel(urls map[string]string, suffix URLSuffix) *DeepInfraModel {
	d := newOpenAIChatDriver(urls, suffix, "deepinfra", deepInfraChatBody)
	d.waitForStreamEOF = true
	d.optionalDiscoveryKey = true
	return &DeepInfraModel{d}
}

func deepInfraChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	body, err := openAIChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	if config != nil {
		if config.DoSample != nil {
			body["do_sample"] = *config.DoSample
		}
		if config.Thinking != nil {
			body["reasoning"] = map[string]any{"enabled": *config.Thinking}
		}
		if config.Effort != nil && *config.Effort != "" && *config.Effort != "default" && (config.Thinking == nil || *config.Thinking) {
			body["reasoning_effort"] = *config.Effort
			if config.Thinking != nil {
				body["reasoning"].(map[string]any)["effort"] = *config.Effort
			}
		}
	}
	return body, nil
}

func (d *DeepInfraModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	if len(texts) > 0 && config != nil && config.Dimension > 0 && config.Dimension < 32 {
		return nil, fmt.Errorf("deepinfra: requested embedding dimension must be at least 32")
	}
	return embedHTTPWithConfig(d.httpClient, d.BaseURL, d.URLSuffix.Embedding, name, texts, api, config)
}

func (d *DeepInfraModel) ListModels(api *APIConfig) ([]string, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	local.APIKey = nil
	resp, err := d.request(http.MethodGet, d.URLSuffix.Models, local, nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result *[]struct {
		ModelName string `json:"model_name"`
	}
	if err := decodeCoHereJSON(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("deepinfra: invalid model list: %w", err)
	}
	if result == nil {
		return nil, fmt.Errorf("deepinfra: missing model list")
	}
	names := make([]string, 0, len(*result))
	seen := map[string]bool{}
	for _, item := range *result {
		if strings.TrimSpace(item.ModelName) == "" || seen[item.ModelName] {
			return nil, fmt.Errorf("deepinfra: invalid or duplicate model name")
		}
		seen[item.ModelName] = true
		names = append(names, item.ModelName)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return names, nil
}

func (d *DeepInfraModel) Balance(api *APIConfig) (map[string]any, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := d.request(http.MethodGet, d.URLSuffix.Balance, local, nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result struct {
		Balance *float64        `json:"stripe_balance"`
		Error   json.RawMessage `json:"error"`
	}
	if err := decodeCoHereJSON(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("deepinfra: invalid balance response: %w", err)
	}
	if result.Balance == nil || len(result.Error) > 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("deepinfra: missing balance or provider error")
	}
	balance := *result.Balance
	// Stripe uses negative values for prepaid funds and positive values for
	// money owed. Preserve the sign of legitimate account balances.
	if math.IsNaN(balance) || math.IsInf(balance, 0) {
		return nil, fmt.Errorf("deepinfra: invalid balance")
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return map[string]any{"balance": balance, "currency": "USD"}, nil
}

// A public model list proves discovery only. Check authenticated account data
// to validate the configured credential without running paid inference.
func (d *DeepInfraModel) CheckConnection(api *APIConfig) error { _, err := d.Balance(api); return err }

var _ ModelDriver = (*DeepInfraModel)(nil)
