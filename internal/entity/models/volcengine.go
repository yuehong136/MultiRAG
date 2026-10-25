// Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
package models

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"maps"
	"net/http"
	"strings"
	"time"
)

// VolcEngine implements Ark chat, discovery and text input to multimodal embedding.
type VolcEngine struct {
	*DummyModel
	httpClient *http.Client
}

func NewVolcEngine(baseURL map[string]string, suffix URLSuffix) *VolcEngine {
	return &VolcEngine{DummyModel: NewDummyModel(maps.Clone(baseURL), suffix), httpClient: &http.Client{Timeout: 120 * time.Second}}
}
func (v *VolcEngine) Name() string { return "volcengine" }

func volcEngineBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	body, err := aliyunChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	delete(body, "enable_thinking")
	if config == nil || config.Thinking == nil {
		return body, nil
	}
	mode := "disabled"
	if *config.Thinking {
		effort := "medium"
		if config.Effort != nil {
			effort = *config.Effort
		}
		switch effort {
		case "none", "minimal":
			effort = "minimal"
		case "auto", "default", "":
			effort, mode = "medium", "enabled"
		case "low", "medium", "high", "xhigh":
			mode = "enabled"
		default:
			return nil, fmt.Errorf("volcengine: invalid effort level")
		}
		body["reasoning_effort"] = effort
	}
	body["thinking"] = map[string]string{"type": mode}
	return body, nil
}

func (v *VolcEngine) request(method, suffix string, config *APIConfig, body interface{}) (*http.Response, error) {
	if config == nil || config.APIKey == nil || strings.TrimSpace(*config.APIKey) == "" {
		return nil, fmt.Errorf("volcengine: API key is required")
	}
	if suffix == "" {
		return nil, fmt.Errorf("volcengine: endpoint suffix is missing")
	}
	baseURL, err := resolveModelBaseURL(v.BaseURL, config.Region)
	if err != nil {
		return nil, err
	}
	key := strings.TrimSpace(*config.APIKey)
	if strings.HasPrefix(key, "{") {
		var stored struct {
			ArkAPIKey string `json:"ark_api_key"`
		}
		if err := json.Unmarshal([]byte(key), &stored); err != nil || strings.TrimSpace(stored.ArkAPIKey) == "" {
			return nil, fmt.Errorf("volcengine: invalid stored API key")
		}
		key = stored.ArkAPIKey
	}
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(requestContext(config), method, joinModelURL(baseURL, suffix), reader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+key)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := v.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("volcengine request: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("volcengine request failed (HTTP %d)", resp.StatusCode)
	}
	return resp, nil
}

func (v *VolcEngine) chat(name string, messages []Message, apiConfig *APIConfig, config *ChatConfig) (*ChatResponse, error) {
	body, err := volcEngineBody(name, messages, config, false)
	if err != nil {
		return nil, err
	}
	resp, err := v.request(http.MethodPost, v.URLSuffix.Chat, apiConfig, body)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result aliyunChatResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("volcengine: decode response: %w", err)
	}
	if len(result.Error) != 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("volcengine: provider returned an error")
	}
	if len(result.Choices) == 0 || result.Choices[0].Message.Content == nil || *result.Choices[0].Message.Content == "" {
		return nil, fmt.Errorf("volcengine: no text answer")
	}
	message := result.Choices[0].Message
	return &ChatResponse{Answer: message.Content, ReasoningContent: message.ReasoningContent}, nil
}
func (v *VolcEngine) ChatWithMessages(name string, apiConfig *APIConfig, messages []Message, config *ChatConfig) (*ChatResponse, error) {
	return v.chat(name, messages, apiConfig, config)
}

func (v *VolcEngine) ChatStreamlyWithSender(name, message *string, apiConfig *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if name == nil || message == nil {
		return fmt.Errorf("model name and message are required")
	}
	return v.ChatStreamlyWithMessages(*name, []Message{{Role: "user", Content: *message}}, apiConfig, config, sender)
}

func (v *VolcEngine) ChatStreamlyWithMessages(name string, messages []Message, apiConfig *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("stream sender is required")
	}
	body, err := volcEngineBody(name, messages, config, true)
	if err != nil {
		return err
	}
	resp, err := v.request(http.MethodPost, v.URLSuffix.Chat, apiConfig, body)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	receivedAnswer := false
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		data := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if data == "" {
			continue
		}
		if data == "[DONE]" {
			if !receivedAnswer {
				return fmt.Errorf("volcengine: stream returned no text answer")
			}
			return sender(&data, nil)
		}
		var event struct {
			Choices []struct {
				Delta struct {
					Content   *string `json:"content"`
					Reasoning *string `json:"reasoning_content"`
				} `json:"delta"`
			} `json:"choices"`
			Error json.RawMessage `json:"error"`
		}
		if err := json.Unmarshal([]byte(data), &event); err != nil {
			return fmt.Errorf("volcengine: decode stream: %w", err)
		}
		if len(event.Error) != 0 && string(event.Error) != "null" {
			return fmt.Errorf("volcengine: provider stream error")
		}
		if len(event.Choices) == 0 {
			continue
		}
		delta := event.Choices[0].Delta
		if delta.Reasoning != nil && *delta.Reasoning != "" {
			if err := sender(nil, delta.Reasoning); err != nil {
				return err
			}
		}
		if delta.Content != nil && *delta.Content != "" {
			if err := sender(delta.Content, nil); err != nil {
				return err
			}
			receivedAnswer = true
		}
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	return io.ErrUnexpectedEOF
}
func (v *VolcEngine) ListModels(config *APIConfig) ([]string, error) {
	local, cancel := modelCallConfig(config)
	defer cancel()
	suffix := strings.Trim(strings.TrimSpace(v.URLSuffix.Models), "/")
	resp, err := v.request(http.MethodGet, suffix, local, nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	// IDs remain exactly as supplied by Ark; owned_by is metadata, not part
	// of the identity selected by a tenant model instance.
	names, err := strictProviderModelNames(resp.Body)
	if err == nil {
		err = requestContext(local).Err()
	}
	return names, err
}

func (v *VolcEngine) CheckConnection(config *APIConfig) error {
	resp, err := v.request(http.MethodGet, v.URLSuffix.Files, config, nil)
	if err != nil {
		return err
	}
	return resp.Body.Close()
}

func (v *VolcEngine) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	if len(texts) == 0 {
		return []EmbeddingData{}, nil
	}
	if name == nil || strings.TrimSpace(*name) == "" {
		return nil, fmt.Errorf("volcengine: embedding model name is required")
	}
	if config != nil && config.Dimension < 0 {
		return nil, fmt.Errorf("embedding dimension must be non-negative")
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	vectors := make([][]float64, 0, len(texts))
	for _, text := range texts {
		body := map[string]any{
			"model":           *name,
			"encoding_format": "float",
			"input":           []map[string]string{{"type": "text", "text": text}},
		}
		response, err := v.request(http.MethodPost, v.URLSuffix.Embedding, local, body)
		if err != nil {
			return nil, err
		}
		var result struct {
			Data *struct {
				Embedding []*float64 `json:"embedding"`
			} `json:"data"`
			Error json.RawMessage `json:"error"`
		}
		decoder := json.NewDecoder(response.Body)
		err = decoder.Decode(&result)
		if err == nil {
			var trailing any
			if decoder.Decode(&trailing) != io.EOF {
				err = fmt.Errorf("trailing embedding response")
			}
		}
		response.Body.Close()
		if err != nil {
			return nil, fmt.Errorf("volcengine: decode embedding: %w", err)
		}
		if len(result.Error) > 0 && string(result.Error) != "null" {
			return nil, fmt.Errorf("volcengine: embedding provider error")
		}
		if result.Data == nil {
			return nil, fmt.Errorf("volcengine: missing embedding data")
		}
		vector := make([]float64, len(result.Data.Embedding))
		for i, value := range result.Data.Embedding {
			if value == nil {
				return nil, fmt.Errorf("volcengine: null embedding value")
			}
			vector[i] = *value
		}
		if config != nil && config.Dimension > 0 && len(vector) != config.Dimension {
			return nil, fmt.Errorf("embedding dimension does not match request")
		}
		if err := ValidateEmbeddings([][]float64{vector}, 1); err != nil {
			return nil, fmt.Errorf("volcengine: %w", err)
		}
		if len(vectors) > 0 && len(vector) != len(vectors[0]) {
			return nil, fmt.Errorf("volcengine: inconsistent embedding dimensions")
		}
		vectors = append(vectors, vector)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return embeddingData(vectors), nil
}
