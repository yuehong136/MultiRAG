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
	"time"
)

// VLLMModel implements standard OpenAI-compatible vLLM chat and discovery.
type VLLMModel struct {
	*DummyModel
	httpClient *http.Client
}

func NewVLLMModel(baseURL map[string]string, suffix URLSuffix) *VLLMModel {
	return &VLLMModel{DummyModel: NewDummyModel(baseURL, suffix), httpClient: &http.Client{Timeout: 120 * time.Second}}
}
func (v *VLLMModel) Name() string { return "vllm" }

func vllmBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	body, err := aliyunChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	delete(body, "enable_thinking")
	if config != nil && config.Thinking != nil {
		body["chat_template_kwargs"] = map[string]bool{"enable_thinking": *config.Thinking}
	}
	return body, nil
}

func (v *VLLMModel) request(method, suffix string, config *APIConfig, body interface{}) (*http.Response, error) {
	if config == nil {
		config = &APIConfig{}
	}
	if suffix == "" {
		return nil, fmt.Errorf("vllm: endpoint suffix is missing")
	}
	baseURL, err := resolveModelBaseURL(v.BaseURL, config.Region)
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
	resp, err := v.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("vllm request: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("vllm request failed (HTTP %d)", resp.StatusCode)
	}
	return resp, nil
}

func (v *VLLMModel) chat(name string, messages []Message, apiConfig *APIConfig, config *ChatConfig) (*ChatResponse, error) {
	body, err := vllmBody(name, messages, config, false)
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
		return nil, fmt.Errorf("vllm: decode response: %w", err)
	}
	if len(result.Error) != 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("vllm: provider returned an error")
	}
	if len(result.Choices) == 0 || result.Choices[0].Message.Content == nil || *result.Choices[0].Message.Content == "" {
		return nil, fmt.Errorf("vllm: no text answer")
	}
	message := result.Choices[0].Message
	return &ChatResponse{Answer: message.Content, ReasoningContent: message.ReasoningContent}, nil
}
func (v *VLLMModel) ChatWithMessages(name string, apiConfig *APIConfig, messages []Message, config *ChatConfig) (*ChatResponse, error) {
	return v.chat(name, messages, apiConfig, config)
}

func (v *VLLMModel) ChatStreamlyWithSender(name, message *string, apiConfig *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if name == nil || message == nil {
		return fmt.Errorf("model name and message are required")
	}
	return v.ChatStreamlyWithMessages(*name, []Message{{Role: "user", Content: *message}}, apiConfig, config, sender)
}

func (v *VLLMModel) ChatStreamlyWithMessages(name string, messages []Message, apiConfig *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("stream sender is required")
	}
	body, err := vllmBody(name, messages, config, true)
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
				return fmt.Errorf("vllm: stream returned no text answer")
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
			return fmt.Errorf("vllm: decode stream: %w", err)
		}
		if len(event.Error) != 0 && string(event.Error) != "null" {
			return fmt.Errorf("vllm: provider stream error")
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
func (v *VLLMModel) ListModels(config *APIConfig) ([]string, error) {
	resp, err := v.request(http.MethodGet, v.URLSuffix.Models, config, nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
		Error json.RawMessage `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, err
	}
	if len(result.Error) != 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("vllm: model list error")
	}
	names := make([]string, 0, len(result.Data))
	for _, item := range result.Data {
		if item.ID != "" {
			names = append(names, item.ID)
		}
	}
	return names, nil
}
func (v *VLLMModel) CheckConnection(config *APIConfig) error {
	names, err := v.ListModels(config)
	if err != nil {
		return err
	}
	if len(names) == 0 {
		return fmt.Errorf("vllm: model list is empty")
	}
	return nil
}
