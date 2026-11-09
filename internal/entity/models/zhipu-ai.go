//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

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

// ZhipuAIModel implements ModelDriver for Zhipu AI
type ZhipuAIModel struct {
	BaseURL    map[string]string
	URLSuffix  URLSuffix
	httpClient *http.Client // Reusable HTTP client with connection pool
}

// NewZhipuAIModel creates a new Zhipu AI model instance
func NewZhipuAIModel(baseURL map[string]string, urlSuffix URLSuffix) *ZhipuAIModel {
	return &ZhipuAIModel{
		BaseURL:    maps.Clone(baseURL),
		URLSuffix:  urlSuffix,
		httpClient: &http.Client{Transport: openAIChatTransport},
	}
}

func (z *ZhipuAIModel) resolveBaseURL(region *string) (string, error) {
	regionName := "default"
	if region != nil && *region != "" {
		regionName = *region
	}

	baseURL := z.BaseURL[regionName]
	if baseURL == "" {
		return "", fmt.Errorf("no base URL configured for region %q", regionName)
	}
	return strings.TrimRight(baseURL, "/"), nil
}

func joinModelURL(baseURL, suffix string) string {
	return baseURL + "/" + strings.TrimLeft(suffix, "/")
}

func (z *ZhipuAIModel) Name() string {
	return "zhipu"
}

// ChatWithMessages sends multiple messages with roles and returns response
func (z *ZhipuAIModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, config *ChatConfig) (*ChatResponse, error) {
	apiConfig, cancelCall := modelCallConfig(apiConfig)
	defer cancelCall()
	body, err := z.historyBody(modelName, messages, config, false)
	if err != nil {
		return nil, err
	}
	resp, err := z.historyRequest(apiConfig, body)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result aliyunChatResponse
	if err := decodeBoundedModelJSON(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("zhipu-ai: decode response: %w", err)
	}
	if len(result.Error) != 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("zhipu-ai: provider error")
	}
	if len(result.Choices) == 0 || result.Choices[0].Message.Content == nil || *result.Choices[0].Message.Content == "" {
		return nil, fmt.Errorf("zhipu-ai: no text answer")
	}
	message := result.Choices[0].Message
	if message.ReasoningContent != nil {
		reason := strings.TrimPrefix(*message.ReasoningContent, "\n")
		message.ReasoningContent = &reason
	}
	return &ChatResponse{Answer: message.Content, ReasoningContent: message.ReasoningContent}, nil
}

func (z *ZhipuAIModel) historyBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
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

func (z *ZhipuAIModel) historyRequest(apiConfig *APIConfig, body map[string]interface{}) (*http.Response, error) {
	if apiConfig == nil || apiConfig.APIKey == nil || strings.TrimSpace(*apiConfig.APIKey) == "" {
		return nil, fmt.Errorf("zhipu-ai: API key is required")
	}
	baseURL, err := z.resolveBaseURL(apiConfig.Region)
	if err != nil {
		return nil, err
	}
	data, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(requestContext(apiConfig), http.MethodPost, joinModelURL(baseURL, z.URLSuffix.Chat), bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*apiConfig.APIKey))
	resp, err := z.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("zhipu-ai: request failed (HTTP %d)", resp.StatusCode)
	}
	return resp, nil
}

func (z *ZhipuAIModel) ChatStreamlyWithSender(name string, messages []Message, apiConfig *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	apiConfig, cancelCall := modelStreamConfig(apiConfig)
	defer cancelCall()
	if sender == nil {
		return fmt.Errorf("zhipu-ai: sender is required")
	}
	body, err := z.historyBody(name, messages, config, true)
	if err != nil {
		return err
	}
	resp, err := z.historyRequest(apiConfig, body)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	receivedAnswer := false
	for {
		data, ok, err := nextOpenAIStreamData(scanner, true)
		if err != nil {
			return err
		}
		if !ok {
			break
		}
		if data == "[DONE]" {
			if err := requestContext(apiConfig).Err(); err != nil {
				return err
			}
			if !receivedAnswer {
				return fmt.Errorf("zhipu-ai: no text answer")
			}
			return sender(&data, nil)
		}
		var event struct {
			Choices []struct {
				Delta struct {
					Content   *string `json:"content"`
					Reasoning *string `json:"reasoning_content"`
				} `json:"delta"`
				FinishReason *string `json:"finish_reason"`
			} `json:"choices"`
			Error json.RawMessage `json:"error"`
		}
		if err := json.Unmarshal([]byte(data), &event); err != nil {
			return fmt.Errorf("zhipu-ai: invalid stream: %w", err)
		}
		if len(event.Error) != 0 && string(event.Error) != "null" {
			return fmt.Errorf("zhipu-ai: stream error")
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
			receivedAnswer = true
			if err := sender(delta.Content, nil); err != nil {
				return err
			}
		}
		if reason := event.Choices[0].FinishReason; reason != nil && *reason != "" {
			if !receivedAnswer {
				return fmt.Errorf("zhipu-ai: no text answer")
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

// Embed uses indexed batches and the shared strict decoder.
func (z *ZhipuAIModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(z.httpClient, z.BaseURL, z.URLSuffix.Embedding, modelName, texts, apiConfig, embeddingConfig)
}

func (z *ZhipuAIModel) ListModels(apiConfig *APIConfig) ([]string, error) {
	return listProviderModels(z.httpClient, z.BaseURL, z.URLSuffix.Models, apiConfig, false)
}

func (z *ZhipuAIModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("balance query is not available for Zhipu AI")
}

func (z *ZhipuAIModel) CheckConnection(apiConfig *APIConfig) error {
	return checkBearerEndpointConnection(z.httpClient, z.BaseURL, z.URLSuffix.Files, apiConfig, z.Name())
}

// Rerank returns validated indexed results, ordered by relevance.
func (z *ZhipuAIModel) Rerank(modelName *string, query string, texts []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return rerankHTTP(z.httpClient, z.BaseURL, z.URLSuffix.Rerank, modelName, query, texts, apiConfig, rerankConfig, nil)
}
