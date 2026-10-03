//
// Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//

package models

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// AliyunModel implements the DashScope OpenAI-compatible chat API.
type AliyunModel struct {
	BaseURL    map[string]string
	URLSuffix  URLSuffix
	httpClient *http.Client
}

// NewAliyunModel creates a DashScope driver for the configured regions.
func NewAliyunModel(baseURL map[string]string, urlSuffix URLSuffix) *AliyunModel {
	return &AliyunModel{
		BaseURL:   baseURL,
		URLSuffix: urlSuffix,
		httpClient: &http.Client{
			Timeout: 120 * time.Second,
			Transport: &http.Transport{
				MaxIdleConns:        100,
				MaxIdleConnsPerHost: 10,
				IdleConnTimeout:     90 * time.Second,
			},
		},
	}
}

func (m *AliyunModel) Name() string { return "aliyun" }

func (m *AliyunModel) endpoint(config *APIConfig, suffix string) (string, string, error) {
	if config == nil || config.APIKey == nil || *config.APIKey == "" {
		return "", "", errors.New("aliyun: API key is required")
	}
	region := "default"
	if config.Region != nil {
		region = *config.Region
	}
	baseURL := m.BaseURL[region]
	if baseURL == "" {
		return "", "", fmt.Errorf("aliyun: no base URL configured for region %q", region)
	}
	if suffix == "" {
		return "", "", errors.New("aliyun: endpoint suffix is missing")
	}
	return strings.TrimRight(baseURL, "/") + "/" + strings.TrimLeft(suffix, "/"), *config.APIKey, nil
}

func aliyunChatBody(modelName string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	if modelName == "" {
		return nil, errors.New("aliyun: model name is required")
	}
	if len(messages) == 0 {
		return nil, errors.New("aliyun: at least one message is required")
	}
	apiMessages := make([]map[string]string, 0, len(messages))
	for _, message := range messages {
		if message.Role == "" {
			return nil, errors.New("aliyun: message role is required")
		}
		apiMessages = append(apiMessages, map[string]string{"role": message.Role, "content": message.Content})
	}
	body := map[string]interface{}{
		"model":       modelName,
		"messages":    apiMessages,
		"stream":      stream,
		"temperature": 1,
	}
	if config == nil {
		return body, nil
	}
	if config.MaxTokens != nil {
		body["max_tokens"] = *config.MaxTokens
	}
	if config.Temperature != nil {
		body["temperature"] = *config.Temperature
	}
	if config.TopP != nil {
		body["top_p"] = *config.TopP
	}
	if config.DoSample != nil {
		body["do_sample"] = *config.DoSample
	}
	if config.Stop != nil {
		body["stop"] = *config.Stop
	}
	if config.Thinking != nil {
		body["enable_thinking"] = *config.Thinking
	}
	return body, nil
}

func (m *AliyunModel) request(method, suffix string, config *APIConfig, body interface{}) (*http.Response, error) {
	endpoint, apiKey, err := m.endpoint(config, suffix)
	if err != nil {
		return nil, err
	}
	var requestBody io.Reader
	if body != nil {
		data, marshalErr := json.Marshal(body)
		if marshalErr != nil {
			return nil, fmt.Errorf("aliyun: encode request: %w", marshalErr)
		}
		requestBody = bytes.NewReader(data)
	}
	req, err := http.NewRequest(method, endpoint, requestBody)
	if err != nil {
		return nil, fmt.Errorf("aliyun: create request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+apiKey)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := m.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("aliyun: send request: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		defer resp.Body.Close()
		responseBody, _ := io.ReadAll(io.LimitReader(resp.Body, 8192))
		return nil, fmt.Errorf("aliyun: API request failed with status %d: %s", resp.StatusCode, strings.TrimSpace(string(responseBody)))
	}
	return resp, nil
}

type aliyunChatResponse struct {
	Choices []struct {
		Message struct {
			Content          *string `json:"content"`
			ReasoningContent *string `json:"reasoning_content"`
		} `json:"message"`
	} `json:"choices"`
	Error json.RawMessage `json:"error"`
}

func (m *AliyunModel) chat(modelName string, messages []Message, apiConfig *APIConfig, chatConfig *ChatConfig) (*ChatResponse, error) {
	body, err := aliyunChatBody(modelName, messages, chatConfig, false)
	if err != nil {
		return nil, err
	}
	resp, err := m.request(http.MethodPost, m.URLSuffix.Chat, apiConfig, body)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result aliyunChatResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("aliyun: decode chat response: %w", err)
	}
	if len(result.Error) != 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("aliyun: chat error: %s", result.Error)
	}
	if len(result.Choices) == 0 || result.Choices[0].Message.Content == nil {
		return nil, errors.New("aliyun: chat response has no answer")
	}
	reasoning := result.Choices[0].Message.ReasoningContent
	if reasoning != nil {
		trimmed := strings.TrimLeft(*reasoning, "\n")
		reasoning = &trimmed
	}
	return &ChatResponse{Answer: result.Choices[0].Message.Content, ReasoningContent: reasoning}, nil
}

// Chat sends a user message and returns the complete answer.
func (m *AliyunModel) Chat(modelName, message *string, apiConfig *APIConfig, chatConfig *ChatConfig) (*ChatResponse, error) {
	if modelName == nil || message == nil {
		return nil, errors.New("aliyun: model name and message are required")
	}
	return m.chat(*modelName, []Message{{Role: "user", Content: *message}}, apiConfig, chatConfig)
}

// ChatWithMessages sends role-tagged messages through the same chat endpoint.
func (m *AliyunModel) ChatWithMessages(modelName string, apiKey *string, messages []Message, chatConfig *ChatConfig) (string, error) {
	response, err := m.chat(modelName, messages, &APIConfig{APIKey: apiKey}, chatConfig)
	if err != nil {
		return "", err
	}
	return *response.Answer, nil
}

func (m *AliyunModel) ChatStreamly(modelName, apiKey, message *string, genConf map[string]interface{}) (<-chan string, error) {
	return nil, errors.New("aliyun: channel streaming is not implemented")
}

func (m *AliyunModel) ChatStreamlyWithChannel(modelName, apiKey, message *string, genConf map[string]interface{}, resultChan chan<- string) error {
	return errors.New("aliyun: channel streaming is not implemented")
}

// ChatStreamlyWithSender sends content and reasoning deltas to sender.
func (m *AliyunModel) ChatStreamlyWithSender(modelName, message *string, apiConfig *APIConfig, chatConfig *ChatConfig, sender func(*string, *string) error) error {
	if modelName == nil || message == nil || sender == nil {
		return errors.New("aliyun: model name, message and sender are required")
	}
	if chatConfig != nil && chatConfig.Stream != nil && !*chatConfig.Stream {
		return errors.New("aliyun: streaming requires stream=true")
	}
	body, err := aliyunChatBody(*modelName, []Message{{Role: "user", Content: *message}}, chatConfig, true)
	if err != nil {
		return err
	}
	resp, err := m.request(http.MethodPost, m.URLSuffix.Chat, apiConfig, body)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		data := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if data == "[DONE]" {
			done := "[DONE]"
			return sender(&done, nil)
		}
		if data == "" {
			continue
		}
		var event struct {
			Choices []struct {
				Delta struct {
					Content          *string `json:"content"`
					ReasoningContent *string `json:"reasoning_content"`
				} `json:"delta"`
			} `json:"choices"`
			Error json.RawMessage `json:"error"`
		}
		if err := json.Unmarshal([]byte(data), &event); err != nil {
			return fmt.Errorf("aliyun: decode stream event: %w", err)
		}
		if len(event.Error) != 0 && string(event.Error) != "null" {
			return fmt.Errorf("aliyun: stream error: %s", event.Error)
		}
		if len(event.Choices) == 0 {
			continue
		}
		delta := event.Choices[0].Delta
		if delta.Content != nil && *delta.Content != "" {
			if err := sender(delta.Content, nil); err != nil {
				return err
			}
		}
		if delta.ReasoningContent != nil && *delta.ReasoningContent != "" {
			if err := sender(nil, delta.ReasoningContent); err != nil {
				return err
			}
		}
	}
	if err := scanner.Err(); err != nil {
		return fmt.Errorf("aliyun: read stream: %w", err)
	}
	return io.ErrUnexpectedEOF
}

func (m *AliyunModel) Encode(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([][]float64, error) {
	return nil, errors.New("aliyun: embeddings are not configured")
}

// ListModels checks the configured endpoint and returns model names.
func (m *AliyunModel) ListModels(apiConfig *APIConfig) ([]string, error) {
	resp, err := m.request(http.MethodGet, m.URLSuffix.Models, apiConfig, nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
		Output struct {
			Models []struct {
				Name string `json:"model_name"`
			} `json:"models"`
		} `json:"output"`
		Error json.RawMessage `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("aliyun: decode model list: %w", err)
	}
	if len(result.Error) != 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("aliyun: model list error: %s", result.Error)
	}
	names := make([]string, 0, len(result.Data)+len(result.Output.Models))
	for _, item := range result.Data {
		if item.ID != "" {
			names = append(names, item.ID)
		}
	}
	for _, item := range result.Output.Models {
		if item.Name != "" {
			names = append(names, item.Name)
		}
	}
	return names, nil
}

func (m *AliyunModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, errors.New("aliyun: balance is not supported")
}

func (m *AliyunModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := m.ListModels(apiConfig)
	return err
}

func (m *AliyunModel) Rerank(modelName *string, query string, texts []string, apiConfig *APIConfig) ([]float64, error) {
	return nil, fmt.Errorf("%s: rerank is not supported", m.Name())
}
