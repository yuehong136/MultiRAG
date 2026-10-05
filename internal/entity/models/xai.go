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
	"net/http"
	"strings"
)

// XAIModel implements standard OpenAI-compatible xAI chat and discovery.
type XAIModel struct {
	*DummyModel
	httpClient *http.Client
}

func NewXAIModel(baseURL map[string]string, suffix URLSuffix) *XAIModel {
	return &XAIModel{DummyModel: NewDummyModel(baseURL, suffix), httpClient: &http.Client{}}
}
func (x *XAIModel) Name() string { return "xai" }

func xaiBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	body, err := aliyunChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	delete(body, "enable_thinking")

	return body, nil
}

func (x *XAIModel) request(method, suffix string, config *APIConfig, body interface{}) (*http.Response, error) {
	if config == nil || config.APIKey == nil || strings.TrimSpace(*config.APIKey) == "" {
		return nil, fmt.Errorf("xai: API key is required")
	}
	if suffix == "" {
		return nil, fmt.Errorf("xai: endpoint suffix is missing")
	}
	baseURL, err := resolveModelBaseURL(x.BaseURL, config.Region)
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
	resp, err := x.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("xai request: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("xai request failed (HTTP %d)", resp.StatusCode)
	}
	return resp, nil
}

func (x *XAIModel) chat(name string, messages []Message, apiConfig *APIConfig, config *ChatConfig) (*ChatResponse, error) {
	body, err := xaiBody(name, messages, config, false)
	if err != nil {
		return nil, err
	}
	local, cancel := modelCallConfig(apiConfig)
	defer cancel()
	resp, err := x.request(http.MethodPost, x.URLSuffix.Chat, local, body)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result aliyunChatResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("xai: decode response: %w", err)
	}
	if len(result.Error) != 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("xai: provider returned an error")
	}
	if len(result.Choices) == 0 || result.Choices[0].Message.Content == nil || *result.Choices[0].Message.Content == "" {
		return nil, fmt.Errorf("xai: no text answer")
	}
	message := result.Choices[0].Message
	return &ChatResponse{Answer: message.Content, ReasoningContent: message.ReasoningContent}, nil
}
func (x *XAIModel) ChatWithMessages(name string, apiConfig *APIConfig, messages []Message, config *ChatConfig) (*ChatResponse, error) {
	return x.chat(name, messages, apiConfig, config)
}

func (x *XAIModel) ChatStreamlyWithSender(name, message *string, apiConfig *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if name == nil || message == nil {
		return fmt.Errorf("model name and message are required")
	}
	return x.ChatStreamlyWithMessages(*name, []Message{{Role: "user", Content: *message}}, apiConfig, config, sender)
}

func (x *XAIModel) ChatStreamlyWithMessages(name string, messages []Message, apiConfig *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("stream sender is required")
	}
	body, err := xaiBody(name, messages, config, true)
	if err != nil {
		return err
	}
	resp, err := x.request(http.MethodPost, x.URLSuffix.Chat, apiConfig, body)
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
				return fmt.Errorf("xai: stream returned no text answer")
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
			return fmt.Errorf("xai: decode stream: %w", err)
		}
		if len(event.Error) != 0 && string(event.Error) != "null" {
			return fmt.Errorf("xai: provider stream error")
		}
		if len(event.Choices) == 0 {
			if len(event.Usage) > 0 && string(event.Usage) != "null" {
				continue
			}
			return fmt.Errorf("xai: stream event has no choices")
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
		if finish := event.Choices[0].FinishReason; finish != nil && *finish != "" {
			if !receivedAnswer {
				return fmt.Errorf("xai: stream returned no text answer")
			}
			if err := requestContext(apiConfig).Err(); err != nil {
				return err
			}
			done := "[DONE]"
			return sender(&done, nil)
		}
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	return io.ErrUnexpectedEOF
}
func (x *XAIModel) ListModels(config *APIConfig) ([]string, error) {
	local, cancel := modelCallConfig(config)
	defer cancel()
	resp, err := x.request(http.MethodGet, x.URLSuffix.Models, local, nil)
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
		return nil, fmt.Errorf("xai: model list error")
	}
	if result.Data == nil {
		return nil, fmt.Errorf("xai: missing model list")
	}
	names := make([]string, 0, len(*result.Data))
	for _, item := range *result.Data {
		if strings.TrimSpace(item.ID) == "" {
			return nil, fmt.Errorf("xai: invalid model ID")
		}
		names = append(names, item.ID)
	}
	return names, nil
}
func (x *XAIModel) CheckConnection(config *APIConfig) error {
	names, err := x.ListModels(config)
	if err != nil {
		return err
	}
	if len(names) == 0 {
		return fmt.Errorf("xai: model list is empty")
	}
	return nil
}
