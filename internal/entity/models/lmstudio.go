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
)

// LMStudioModel implements standard OpenAI-compatible LM Studio chat and discovery.
type LMStudioModel struct {
	*DummyModel
	httpClient *http.Client
}

func NewLMStudioModel(baseURL map[string]string, suffix URLSuffix) *LMStudioModel {
	return &LMStudioModel{DummyModel: NewDummyModel(maps.Clone(baseURL), suffix), httpClient: &http.Client{}}
}
func (l *LMStudioModel) Name() string { return "LM-Studio" }

func lmStudioBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	if strings.TrimSpace(name) == "" {
		return nil, fmt.Errorf("lmstudio: model name is required")
	}
	body, err := aliyunChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	delete(body, "enable_thinking")
	if config != nil && config.Thinking != nil {
		mode := "disabled"
		if *config.Thinking {
			mode = "enabled"
		}
		body["thinking"] = map[string]string{"type": mode}
	}

	return body, nil
}

func (l *LMStudioModel) request(method, suffix string, config *APIConfig, body interface{}) (*http.Response, error) {
	if config == nil {
		config = &APIConfig{}
	}
	if suffix == "" {
		return nil, fmt.Errorf("lmstudio: endpoint suffix is missing")
	}
	baseURL, err := resolveModelBaseURL(l.BaseURL, config.Region)
	if err != nil {
		return nil, err
	}
	key := ""
	if config.APIKey != nil {
		key = strings.TrimSpace(*config.APIKey)
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
	if key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := l.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("lmstudio request: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("lmstudio request failed (HTTP %d)", resp.StatusCode)
	}
	return resp, nil
}

func (l *LMStudioModel) chat(name string, messages []Message, apiConfig *APIConfig, config *ChatConfig) (*ChatResponse, error) {
	body, err := lmStudioBody(name, messages, config, false)
	if err != nil {
		return nil, err
	}
	local, cancel := modelCallConfig(apiConfig)
	defer cancel()
	resp, err := l.request(http.MethodPost, l.URLSuffix.Chat, local, body)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result aliyunChatResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("lmstudio: decode response: %w", err)
	}
	if len(result.Error) != 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("lmstudio: provider returned an error")
	}
	if len(result.Choices) == 0 || result.Choices[0].Message.Content == nil || *result.Choices[0].Message.Content == "" {
		return nil, fmt.Errorf("lmstudio: no text answer")
	}
	message := result.Choices[0].Message
	answer, reasoning := message.Content, message.ReasoningContent
	if reasoning == nil {
		class := ModelClassFromName(name)
		if config != nil && config.ModelClass != nil {
			class = strings.ToLower(*config.ModelClass)
		}
		reasoning, answer = GetThinkingAndAnswer(&class, answer)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	if answer == nil || strings.TrimSpace(*answer) == "" {
		return nil, fmt.Errorf("lmstudio: no text answer")
	}
	return &ChatResponse{Answer: answer, ReasoningContent: reasoning}, nil
}
func (l *LMStudioModel) ChatWithMessages(name string, apiConfig *APIConfig, messages []Message, config *ChatConfig) (*ChatResponse, error) {
	return l.chat(name, messages, apiConfig, config)
}

func (l *LMStudioModel) ChatStreamlyWithSender(name, message *string, apiConfig *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if name == nil || message == nil {
		return fmt.Errorf("model name and message are required")
	}
	return l.ChatStreamlyWithMessages(*name, []Message{{Role: "user", Content: *message}}, apiConfig, config, sender)
}

func (l *LMStudioModel) ChatStreamlyWithMessages(name string, messages []Message, apiConfig *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("stream sender is required")
	}
	body, err := lmStudioBody(name, messages, config, true)
	if err != nil {
		return err
	}
	resp, err := l.request(http.MethodPost, l.URLSuffix.Chat, apiConfig, body)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	receivedAnswer := false
	for scanner.Scan() {
		if err := requestContext(apiConfig).Err(); err != nil {
			return err
		}
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
				return fmt.Errorf("lmstudio: stream returned no text answer")
			}
			if err := requestContext(apiConfig).Err(); err != nil {
				return err
			}
			return sender(&data, nil)
		}
		var event struct {
			Choices []struct {
				FinishReason *string `json:"finish_reason"`
				Delta        struct {
					Content   *string `json:"content"`
					Reasoning *string `json:"reasoning_content"`
				} `json:"delta"`
			} `json:"choices"`
			Error json.RawMessage `json:"error"`
			Usage json.RawMessage `json:"usage"`
		}
		if err := json.Unmarshal([]byte(data), &event); err != nil {
			return fmt.Errorf("lmstudio: decode stream: %w", err)
		}
		if len(event.Error) != 0 && string(event.Error) != "null" {
			return fmt.Errorf("lmstudio: provider stream error")
		}
		if len(event.Choices) == 0 {
			if len(event.Usage) > 0 && string(event.Usage) != "null" {
				continue
			}
			return fmt.Errorf("lmstudio: stream event has no choices")
		}
		delta := event.Choices[0].Delta
		if delta.Reasoning != nil && *delta.Reasoning != "" {
			if err := sender(nil, delta.Reasoning); err != nil {
				return err
			}
		}
		if err := requestContext(apiConfig).Err(); err != nil {
			return err
		}
		if delta.Content != nil && *delta.Content != "" {
			if err := sender(delta.Content, nil); err != nil {
				return err
			}
			receivedAnswer = receivedAnswer || strings.TrimSpace(*delta.Content) != ""
		}
		if finish := event.Choices[0].FinishReason; finish != nil && *finish != "" {
			if !receivedAnswer {
				return fmt.Errorf("lmstudio: stream returned no text answer")
			}
			if err := requestContext(apiConfig).Err(); err != nil {
				return err
			}
			done := "[DONE]"
			return sender(&done, nil)
		}
	}
	if err := requestContext(apiConfig).Err(); err != nil {
		return err
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	return io.ErrUnexpectedEOF
}
func (l *LMStudioModel) ListModels(config *APIConfig) ([]string, error) {
	local, cancel := modelCallConfig(config)
	defer cancel()
	resp, err := l.request(http.MethodGet, l.URLSuffix.Models, local, nil)
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
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, err
	}
	if len(result.Error) != 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("lmstudio: model list error")
	}
	if result.Data == nil {
		return nil, fmt.Errorf("lmstudio: missing model list")
	}
	names := make([]string, 0, len(*result.Data))
	for _, item := range *result.Data {
		if strings.TrimSpace(item.ID) == "" {
			return nil, fmt.Errorf("lmstudio: invalid model ID")
		}
		names = append(names, item.ID)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return names, nil
}

func (l *LMStudioModel) Encode(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([][]float64, error) {
	return encodeHTTPWithConfig(l.httpClient, l.BaseURL, l.URLSuffix.Embedding, name, texts, api, config, embeddingHTTPOptions{optionalAPIKey: true, fallbackRegion: true})
}
func (l *LMStudioModel) Rerank(name *string, query string, texts []string, api *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return nil, fmt.Errorf("lmstudio: rerank is not implemented")
}
func (l *LMStudioModel) Balance(api *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("lmstudio: balance is not implemented")
}
func (l *LMStudioModel) CheckConnection(api *APIConfig) error {
	_, err := l.ListModels(api)
	return err
}

var _ ModelDriver = (*LMStudioModel)(nil)
