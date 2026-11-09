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
	"bytes"
	"encoding/json"
	"fmt"
	"maps"
	"net/http"
	"strings"
)

// SiliconFlowModel implements ModelDriver for SiliconFlow.
type SiliconFlowModel struct {
	BaseURL    map[string]string
	URLSuffix  URLSuffix
	httpClient *http.Client // Reusable HTTP client with connection pool
}

// NewSiliconFlowModel creates a new SiliconFlow model instance.
func NewSiliconFlowModel(baseURL map[string]string, urlSuffix URLSuffix) *SiliconFlowModel {
	return &SiliconFlowModel{
		BaseURL:    maps.Clone(baseURL),
		URLSuffix:  urlSuffix,
		httpClient: &http.Client{Transport: openAIChatTransport},
	}
}

func (m *SiliconFlowModel) Name() string {
	return "siliconflow"
}

func (m *SiliconFlowModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	apiConfig, cancelCall := modelCallConfig(apiConfig)
	defer cancelCall()
	if strings.TrimSpace(modelName) == "" {
		return nil, fmt.Errorf("model is required")
	}
	if err := ValidateMessages(messages); err != nil {
		return nil, err
	}
	if apiConfig == nil || apiConfig.APIKey == nil || strings.TrimSpace(*apiConfig.APIKey) == "" {
		return nil, fmt.Errorf("API key is nil")
	}
	chatModelConfig = normalizeChatConfig(chatModelConfig)

	baseURL, err := resolveModelBaseURL(m.BaseURL, apiConfig.Region)
	if err != nil {
		return nil, err
	}
	if m.URLSuffix.Chat == "" {
		return nil, fmt.Errorf("siliconflow: chat endpoint is missing")
	}
	url := joinModelURL(baseURL, m.URLSuffix.Chat)

	modelClass := chatModelClass(modelName, chatModelConfig)
	if (modelClass == "qwen" || modelClass == "glm") && m.URLSuffix.AsyncChat != "" {
		url = joinModelURL(baseURL, m.URLSuffix.AsyncChat)
	}

	// Build request body
	reqBody := map[string]interface{}{
		"model":       modelName,
		"messages":    messages,
		"stream":      false,
		"temperature": 1,
	}

	if chatModelConfig.MaxTokens != nil {
		reqBody["max_tokens"] = *chatModelConfig.MaxTokens
	}

	if chatModelConfig.Temperature != nil {
		reqBody["temperature"] = *chatModelConfig.Temperature
	}

	if chatModelConfig.TopP != nil {
		reqBody["top_p"] = *chatModelConfig.TopP
	}

	if chatModelConfig.Stop != nil {
		reqBody["stop"] = *chatModelConfig.Stop
	}

	if chatModelConfig.Thinking != nil {
		if *chatModelConfig.Thinking {
			reqBody["thinking"] = map[string]interface{}{
				"type": "enabled",
			}
		} else {
			reqBody["thinking"] = map[string]interface{}{
				"type": "disabled",
			}
		}
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(requestContext(apiConfig), "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*apiConfig.APIKey))

	resp, err := m.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("API request failed with status %d", resp.StatusCode)
	}
	response, err := decodeLegacyChatResponse(resp.Body)
	if ctxErr := requestContext(apiConfig).Err(); ctxErr != nil {
		return nil, ctxErr
	}
	if err != nil {
		return nil, err
	}
	thinking, answer := GetThinkingAndAnswer(&modelClass, response.Answer)
	if response.ReasoningContent != nil && *response.ReasoningContent != "" {
		reason := strings.TrimPrefix(*response.ReasoningContent, "\n")
		thinking, answer = &reason, response.Answer
	}
	if answer == nil || strings.TrimSpace(*answer) == "" {
		return nil, fmt.Errorf("model chat response has no text answer")
	}
	return &ChatResponse{Answer: answer, ReasoningContent: thinking}, nil
}

// ChatWithMessages sends multiple messages with roles and returns response

func (m *SiliconFlowModel) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig, sender func(*string, *string) error) error {
	apiConfig, cancelCall := modelStreamConfig(apiConfig)
	defer cancelCall()
	if sender == nil || apiConfig == nil || apiConfig.APIKey == nil || strings.TrimSpace(*apiConfig.APIKey) == "" {
		return fmt.Errorf("message or API key is nil")
	}
	if strings.TrimSpace(modelName) == "" {
		return fmt.Errorf("model is required")
	}
	if err := ValidateMessages(messages); err != nil {
		return err
	}
	chatModelConfig = normalizeChatConfig(chatModelConfig)
	baseURL, err := resolveModelBaseURL(m.BaseURL, apiConfig.Region)
	if err != nil {
		return err
	}
	if m.URLSuffix.Chat == "" {
		return fmt.Errorf("siliconflow: chat endpoint is missing")
	}
	url := joinModelURL(baseURL, m.URLSuffix.Chat)

	// Build request body with streaming enabled
	reqBody := map[string]interface{}{
		"model":       modelName,
		"messages":    messages,
		"stream":      true,
		"temperature": 1,
	}

	if chatModelConfig.MaxTokens != nil {
		reqBody["max_tokens"] = *chatModelConfig.MaxTokens
	}

	if chatModelConfig.Temperature != nil {
		reqBody["temperature"] = *chatModelConfig.Temperature
	}

	if chatModelConfig.DoSample != nil {
		reqBody["do_sample"] = *chatModelConfig.DoSample
	}

	if chatModelConfig.TopP != nil {
		reqBody["top_p"] = *chatModelConfig.TopP
	}

	if chatModelConfig.Stop != nil {
		reqBody["stop"] = *chatModelConfig.Stop
	}

	if chatModelConfig.Thinking != nil {
		if *chatModelConfig.Thinking {
			reqBody["thinking"] = map[string]interface{}{
				"type": "enabled",
			}
		} else {
			reqBody["thinking"] = map[string]interface{}{
				"type": "disabled",
			}
		}
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return fmt.Errorf("failed to marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(requestContext(apiConfig), "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*apiConfig.APIKey))

	resp, err := m.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("API request failed with status %d", resp.StatusCode)
	}

	return readLegacyChatStream(resp.Body, apiConfig, false, sender)
}

// Embed returns indexed embeddings for a list of texts
func (m *SiliconFlowModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(m.httpClient, m.BaseURL, m.URLSuffix.Embedding, modelName, texts, apiConfig, embeddingConfig)
}

func (m *SiliconFlowModel) ListModels(apiConfig *APIConfig) ([]string, error) {
	if apiConfig == nil || apiConfig.APIKey == nil || strings.TrimSpace(*apiConfig.APIKey) == "" {
		return nil, fmt.Errorf("siliconflow: model discovery API key is required")
	}
	return listProviderModels(m.httpClient, m.BaseURL, m.URLSuffix.Models, apiConfig, true)
}

type siliconFlowBalanceResponse struct {
	Code   *int  `json:"code"`
	Status *bool `json:"status"`
	Data   struct {
		Balance      string `json:"balance"`
		TotalBalance string `json:"totalBalance"`
	} `json:"data"`
	Error json.RawMessage `json:"error"`
}

func (m *SiliconFlowModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	var result siliconFlowBalanceResponse
	if err := decodeBalanceHTTP(m.httpClient, m.BaseURL, m.URLSuffix.Balance, apiConfig, &result); err != nil {
		return nil, fmt.Errorf("siliconflow: %w", err)
	}
	if result.Code == nil || *result.Code != 0 || result.Status == nil || !*result.Status || len(result.Error) != 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("siliconflow: balance provider returned an error")
	}
	raw := result.Data.TotalBalance
	if raw == "" {
		raw = result.Data.Balance
	}
	amount, err := parseBalanceAmount(raw)
	if err != nil {
		return nil, fmt.Errorf("siliconflow: %w", err)
	}
	return map[string]interface{}{"balance": amount, "currency": "CNY"}, nil
}

func (m *SiliconFlowModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := m.ListModels(apiConfig)
	if err != nil {
		return err
	}
	return nil
}

func (m *SiliconFlowModel) Rerank(name *string, query string, texts []string, config *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return rerankHTTP(m.httpClient, m.BaseURL, m.URLSuffix.Rerank, name, query, texts, config, rerankConfig, map[string]any{"max_chunks_per_doc": 1024, "overlap_tokens": 80})
}
