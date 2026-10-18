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

// openAIChatDriver shares the HTTP and response contract of the hosted chat providers.
type openAIChatDriver struct {
	*DummyModel
	httpClient          *http.Client
	provider            string
	buildBody           func(string, []Message, *ChatConfig, bool) (map[string]interface{}, error)
	splitReasoning      bool
	openRouterReasoning bool
}

// One pool is shared across request-scoped drivers; credentials stay on requests.
var openAIChatTransport = func() *http.Transport {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.MaxIdleConns = 100
	transport.MaxIdleConnsPerHost = 10
	transport.IdleConnTimeout = 90 * time.Second
	transport.ResponseHeaderTimeout = 60 * time.Second
	return transport
}()

func newOpenAIChatDriver(baseURL map[string]string, suffix URLSuffix, provider string, buildBody func(string, []Message, *ChatConfig, bool) (map[string]interface{}, error)) *openAIChatDriver {
	return &openAIChatDriver{DummyModel: NewDummyModel(maps.Clone(baseURL), suffix), httpClient: &http.Client{Transport: openAIChatTransport}, provider: provider, buildBody: buildBody}
}
func (d *openAIChatDriver) Name() string { return d.provider }

func openAIChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	if strings.TrimSpace(name) == "" {
		return nil, fmt.Errorf("model name is required")
	}
	body, err := aliyunChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	delete(body, "enable_thinking")
	delete(body, "do_sample")
	return body, nil
}

func (d *openAIChatDriver) request(method, suffix string, config *APIConfig, body interface{}) (*http.Response, error) {
	if config == nil || config.APIKey == nil || strings.TrimSpace(*config.APIKey) == "" {
		return nil, fmt.Errorf("%s: API key is required", d.Name())
	}
	if suffix == "" {
		return nil, fmt.Errorf("openai-compatible: endpoint suffix is missing")
	}
	baseURL, err := resolveModelBaseURL(d.BaseURL, config.Region)
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
	resp, err := d.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("openai-compatible request: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("openai-compatible request failed (HTTP %d)", resp.StatusCode)
	}
	return resp, nil
}

func (d *openAIChatDriver) chat(name string, messages []Message, apiConfig *APIConfig, config *ChatConfig) (*ChatResponse, error) {
	body, err := d.buildBody(name, messages, config, false)
	if err != nil {
		return nil, err
	}
	local, cancel := modelCallConfig(apiConfig)
	defer cancel()
	resp, err := d.request(http.MethodPost, d.URLSuffix.Chat, local, body)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result struct {
		Choices []struct {
			Message struct {
				Content          *string `json:"content"`
				ReasoningContent *string `json:"reasoning_content"`
				Reasoning        *string `json:"reasoning"`
			} `json:"message"`
		} `json:"choices"`
		Error json.RawMessage `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("openai-compatible: decode response: %w", err)
	}
	if len(result.Error) != 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("openai-compatible: provider returned an error")
	}
	if len(result.Choices) == 0 || result.Choices[0].Message.Content == nil || *result.Choices[0].Message.Content == "" {
		return nil, fmt.Errorf("openai-compatible: no text answer")
	}
	message := result.Choices[0].Message
	answer, reasoning := message.Content, message.ReasoningContent
	if d.openRouterReasoning && message.Reasoning != nil {
		reasoning = message.Reasoning
	}
	if reasoning == nil && d.splitReasoning {
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
		return nil, fmt.Errorf("openai-compatible: no text answer")
	}
	return &ChatResponse{Answer: answer, ReasoningContent: reasoning}, nil
}
func (d *openAIChatDriver) ChatWithMessages(name string, apiConfig *APIConfig, messages []Message, config *ChatConfig) (*ChatResponse, error) {
	return d.chat(name, messages, apiConfig, config)
}

func (d *openAIChatDriver) ChatStreamlyWithSender(name, message *string, apiConfig *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if name == nil || message == nil {
		return fmt.Errorf("model name and message are required")
	}
	return d.ChatStreamlyWithMessages(*name, []Message{{Role: "user", Content: *message}}, apiConfig, config, sender)
}

func (d *openAIChatDriver) ChatStreamlyWithMessages(name string, messages []Message, apiConfig *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("stream sender is required")
	}
	body, err := d.buildBody(name, messages, config, true)
	if err != nil {
		return err
	}
	resp, err := d.request(http.MethodPost, d.URLSuffix.Chat, apiConfig, body)
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
				return fmt.Errorf("openai-compatible: stream returned no text answer")
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
					Content         *string `json:"content"`
					Reasoning       *string `json:"reasoning_content"`
					RouterReasoning *string `json:"reasoning"`
				} `json:"delta"`
			} `json:"choices"`
			Error json.RawMessage `json:"error"`
			Usage json.RawMessage `json:"usage"`
		}
		if err := json.Unmarshal([]byte(data), &event); err != nil {
			return fmt.Errorf("openai-compatible: decode stream: %w", err)
		}
		if len(event.Error) != 0 && string(event.Error) != "null" {
			return fmt.Errorf("openai-compatible: provider stream error")
		}
		if len(event.Choices) == 0 {
			if len(event.Usage) > 0 && string(event.Usage) != "null" {
				continue
			}
			return fmt.Errorf("openai-compatible: stream event has no choices")
		}
		delta := event.Choices[0].Delta
		if d.openRouterReasoning && delta.RouterReasoning != nil {
			delta.Reasoning = delta.RouterReasoning
		}
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
				return fmt.Errorf("openai-compatible: stream returned no text answer")
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
func (d *openAIChatDriver) ListModels(config *APIConfig) ([]string, error) {
	local, cancel := modelCallConfig(config)
	defer cancel()
	resp, err := d.request(http.MethodGet, d.URLSuffix.Models, local, nil)
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
		return nil, fmt.Errorf("openai-compatible: model list error")
	}
	if result.Data == nil {
		return nil, fmt.Errorf("openai-compatible: missing model list")
	}
	names := make([]string, 0, len(*result.Data))
	for _, item := range *result.Data {
		if strings.TrimSpace(item.ID) == "" {
			return nil, fmt.Errorf("openai-compatible: invalid model ID")
		}
		names = append(names, item.ID)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return names, nil
}

// These capabilities are deliberately unavailable in this driver batch.
func (d *openAIChatDriver) Encode(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([][]float64, error) {
	return nil, fmt.Errorf("openai-compatible: embedding is not implemented")
}
func (d *openAIChatDriver) Rerank(name *string, query string, texts []string, api *APIConfig) ([]float64, error) {
	return nil, fmt.Errorf("openai-compatible: rerank is not implemented")
}
func (d *openAIChatDriver) Balance(api *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("openai-compatible: balance is not implemented")
}
func (d *openAIChatDriver) CheckConnection(api *APIConfig) error {
	_, err := d.ListModels(api)
	return err
}

var _ ModelDriver = (*openAIChatDriver)(nil)
