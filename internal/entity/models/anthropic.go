// Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
// Licensed under the Apache License, Version 2.0.
package models

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"maps"
	"net/http"
	"strings"
)

const anthropicVersion = "2023-06-01"

// AnthropicModel uses the native Messages API. Streaming and media operations
// remain unavailable unless their optional capability is implemented.
type AnthropicModel struct {
	*DummyModel
	httpClient *http.Client
}

func NewAnthropicModel(urls map[string]string, suffix URLSuffix) *AnthropicModel {
	return &AnthropicModel{NewDummyModel(maps.Clone(urls), suffix), &http.Client{Transport: openAIChatTransport}}
}

func (a *AnthropicModel) Name() string { return "anthropic" }

func (a *AnthropicModel) request(method, suffix string, api *APIConfig, body any) (*http.Response, error) {
	if api == nil || api.APIKey == nil || strings.TrimSpace(*api.APIKey) == "" {
		return nil, fmt.Errorf("anthropic: API key is required")
	}
	if suffix == "" {
		return nil, fmt.Errorf("anthropic: endpoint suffix is missing")
	}
	base, err := resolveModelBaseURL(a.BaseURL, api.Region)
	if err != nil {
		return nil, err
	}
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return nil, fmt.Errorf("anthropic: invalid request JSON")
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(requestContext(api), method, joinModelURL(base, suffix), reader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("x-api-key", strings.TrimSpace(*api.APIKey))
	req.Header.Set("anthropic-version", anthropicVersion)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("anthropic request: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("anthropic request failed (HTTP %d)", resp.StatusCode)
	}
	return resp, nil
}

func (a *AnthropicModel) ChatWithMessages(name string, api *APIConfig, messages []Message, config *ChatConfig) (*ChatResponse, error) {
	if strings.TrimSpace(name) == "" {
		return nil, fmt.Errorf("anthropic: model name is required")
	}
	converted, system, err := anthropicMessages(messages)
	if err != nil {
		return nil, err
	}
	body := map[string]any{"model": name, "messages": converted, "max_tokens": 1024}
	if system != "" {
		body["system"] = system
	}
	if config != nil {
		if config.Thinking != nil && *config.Thinking {
			body["max_tokens"] = 2048
			body["thinking"] = map[string]any{"type": "enabled", "budget_tokens": 1024}
		} else if config.Thinking != nil {
			body["thinking"] = map[string]any{"type": "disabled"}
		}
		if config.MaxTokens != nil {
			if *config.MaxTokens <= 0 || config.Thinking != nil && *config.Thinking && *config.MaxTokens <= 1024 {
				return nil, fmt.Errorf("anthropic: invalid max_tokens for thinking budget")
			}
			body["max_tokens"] = *config.MaxTokens
		}
		if config.Temperature != nil {
			body["temperature"] = *config.Temperature
		}
		if config.TopP != nil {
			body["top_p"] = *config.TopP
		}
		if config.Stop != nil {
			body["stop_sequences"] = *config.Stop
		}
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := a.request(http.MethodPost, a.URLSuffix.Chat, local, body)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result struct {
		Content []struct {
			Type     string  `json:"type"`
			Text     *string `json:"text"`
			Thinking *string `json:"thinking"`
		} `json:"content"`
		Error json.RawMessage `json:"error"`
	}
	if err := decodeCoHereJSON(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("anthropic: invalid chat response: %w", err)
	}
	if len(result.Error) > 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("anthropic: provider returned an error")
	}
	var answer, reasoning strings.Builder
	for _, block := range result.Content {
		switch block.Type {
		case "text":
			if block.Text == nil {
				return nil, fmt.Errorf("anthropic: missing text")
			}
			answer.WriteString(*block.Text)
		case "thinking":
			if block.Thinking == nil {
				return nil, fmt.Errorf("anthropic: missing thinking text")
			}
			reasoning.WriteString(*block.Thinking)
		case "redacted_thinking":
			// Redacted provider thinking is not user-visible text.
		default:
			return nil, fmt.Errorf("anthropic: unsupported response content block")
		}
	}
	if strings.TrimSpace(answer.String()) == "" {
		return nil, fmt.Errorf("anthropic: no text answer")
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	text, thought := answer.String(), reasoning.String()
	return &ChatResponse{Answer: &text, ReasoningContent: &thought}, nil
}

func anthropicMessages(messages []Message) ([]map[string]any, string, error) {
	converted := make([]map[string]any, 0, len(messages))
	var systems []string
	for _, message := range messages {
		role := strings.ToLower(strings.TrimSpace(message.Role))
		content, err := anthropicContent(message.Content)
		if err != nil {
			return nil, "", err
		}
		switch role {
		case "system":
			switch value := content.(type) {
			case string:
				systems = append(systems, value)
			case []map[string]any:
				var parts []string
				for _, block := range value {
					if block["type"] != "text" {
						return nil, "", fmt.Errorf("anthropic: system content must be text")
					}
					parts = append(parts, block["text"].(string))
				}
				systems = append(systems, strings.Join(parts, "\n"))
			}
		case "user", "assistant":
			converted = append(converted, map[string]any{"role": role, "content": content})
		default:
			return nil, "", fmt.Errorf("anthropic: unsupported message role")
		}
	}
	if len(converted) == 0 {
		return nil, "", fmt.Errorf("anthropic: messages are empty")
	}
	return converted, strings.Join(systems, "\n\n"), nil
}

func anthropicContent(content any) (any, error) {
	var blocks []any
	switch value := content.(type) {
	case string:
		if strings.TrimSpace(value) == "" {
			return nil, fmt.Errorf("anthropic: empty message content")
		}
		return value, nil
	case []any:
		blocks = value
	case []map[string]any:
		for _, block := range value {
			blocks = append(blocks, block)
		}
	default:
		return nil, fmt.Errorf("anthropic: unsupported message content")
	}
	if len(blocks) == 0 {
		return nil, fmt.Errorf("anthropic: empty message content")
	}
	converted := make([]map[string]any, 0, len(blocks))
	for _, item := range blocks {
		block, ok := item.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("anthropic: invalid content block")
		}
		switch block["type"] {
		case "text":
			text, ok := block["text"].(string)
			if !ok || strings.TrimSpace(text) == "" {
				return nil, fmt.Errorf("anthropic: invalid text block")
			}
			converted = append(converted, map[string]any{"type": "text", "text": text})
		case "image", "image_url":
			source, ok := block["source"].(map[string]any)
			if block["type"] == "image_url" {
				image, valid := block["image_url"].(map[string]any)
				if !valid {
					return nil, fmt.Errorf("anthropic: invalid image_url")
				}
				value, valid := image["url"].(string)
				if !valid {
					return nil, fmt.Errorf("anthropic: invalid image URL")
				}
				source = map[string]any{"type": "url", "url": value}
				if strings.HasPrefix(value, "data:") {
					parts := strings.SplitN(strings.TrimPrefix(value, "data:"), ";base64,", 2)
					if len(parts) != 2 {
						return nil, fmt.Errorf("anthropic: invalid data image URL")
					}
					source = map[string]any{"type": "base64", "media_type": parts[0], "data": parts[1]}
				}
				ok = true
			}
			if !ok {
				return nil, fmt.Errorf("anthropic: image source is required")
			}
			clean, err := anthropicImageSource(source)
			if err != nil {
				return nil, err
			}
			converted = append(converted, map[string]any{"type": "image", "source": clean})
		default:
			return nil, fmt.Errorf("anthropic: unsupported content block")
		}
	}
	return converted, nil
}

func anthropicImageSource(source map[string]any) (map[string]any, error) {
	switch source["type"] {
	case "url":
		value, ok := source["url"].(string)
		if !ok {
			return nil, fmt.Errorf("anthropic: invalid image URL")
		}
		if err := ValidateOCRURL(value); err != nil {
			return nil, err
		}
		return map[string]any{"type": "url", "url": value}, nil
	case "base64":
		mime, ok := source["media_type"].(string)
		if !ok || mime != "image/jpeg" && mime != "image/png" && mime != "image/gif" && mime != "image/webp" {
			return nil, fmt.Errorf("anthropic: unsupported image MIME")
		}
		data, ok := source["data"].(string)
		if !ok {
			return nil, fmt.Errorf("anthropic: missing image data")
		}
		if _, err := DecodeMediaData(data); err != nil {
			return nil, err
		}
		return map[string]any{"type": "base64", "media_type": mime, "data": data}, nil
	default:
		return nil, fmt.Errorf("anthropic: unsupported image source")
	}
}

func (a *AnthropicModel) ListModels(api *APIConfig) ([]string, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := a.request(http.MethodGet, a.URLSuffix.Models, local, nil)
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
		return nil, fmt.Errorf("anthropic: invalid model list: %w", err)
	}
	if len(result.Error) > 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("anthropic: model list error")
	}
	if result.Data == nil {
		return nil, fmt.Errorf("anthropic: missing model list")
	}
	names := make([]string, 0, len(*result.Data))
	seen := map[string]bool{}
	for _, item := range *result.Data {
		if strings.TrimSpace(item.ID) == "" || seen[item.ID] {
			return nil, fmt.Errorf("anthropic: invalid or duplicate model ID")
		}
		seen[item.ID] = true
		names = append(names, item.ID)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return names, nil
}

func (a *AnthropicModel) CheckConnection(api *APIConfig) error {
	_, err := a.ListModels(api)
	return err
}

var _ ModelDriver = (*AnthropicModel)(nil)
