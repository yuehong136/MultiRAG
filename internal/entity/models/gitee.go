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
	"io"
	"maps"
	"net/http"
	"strings"
	"time"
)

// GiteeModel implements ModelDriver for Gitee
type GiteeModel struct {
	BaseURL    map[string]string
	URLSuffix  URLSuffix
	httpClient *http.Client // Reusable HTTP client with connection pool
}

// NewGiteeModel creates a new Gitee model instance
func NewGiteeModel(baseURL map[string]string, urlSuffix URLSuffix) *GiteeModel {
	return &GiteeModel{
		BaseURL:   maps.Clone(baseURL),
		URLSuffix: urlSuffix,
		httpClient: &http.Client{
			Transport: &http.Transport{
				MaxIdleConns:        100,
				MaxIdleConnsPerHost: 10,
				IdleConnTimeout:     90 * time.Second,
				DisableCompression:  false,
			},
		},
	}
}

func (m *GiteeModel) Name() string {
	return "gitee"
}

// Chat sends a message and returns response
func (m *GiteeModel) Chat(modelName, message *string, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	if modelName == nil || message == nil {
		return nil, fmt.Errorf("model and message are required")
	}
	return m.ChatWithMessages(*modelName, apiConfig, []Message{{Role: "user", Content: *message}}, chatModelConfig)
}
func (m *GiteeModel) ChatWithMessages(modelName string, apiConfig *APIConfig, messages []Message, chatModelConfig *ChatConfig) (*ChatResponse, error) {
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
		return nil, fmt.Errorf("chat endpoint is missing")
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

func (m *GiteeModel) ChatStreamly(modelName, apiKey, message *string, genConf map[string]interface{}) (<-chan string, error) {
	return nil, fmt.Errorf("streaming chat is not implemented for %s", m.Name())
}

func (m *GiteeModel) ChatStreamlyWithChannel(modelName, apiKey, message *string, genConf map[string]interface{}, resultChan chan<- string) error {
	return fmt.Errorf("streaming chat is not implemented for %s", m.Name())
}

// ChatStreamlyWithSender sends a message and streams response via sender function (best performance, no channel)
func (m *GiteeModel) ChatStreamlyWithSender(modelName, message *string, apiConfig *APIConfig, chatModelConfig *ChatConfig, sender func(*string, *string) error) error {
	if modelName == nil || message == nil {
		return fmt.Errorf("model and message are required")
	}
	return m.ChatStreamlyWithMessages(*modelName, []Message{{Role: "user", Content: *message}}, apiConfig, chatModelConfig, sender)
}
func (m *GiteeModel) ChatStreamlyWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig, sender func(*string, *string) error) error {
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
		return fmt.Errorf("chat endpoint is missing")
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

	// Reuse the incremental splitter so tags split across provider chunks do
	// not leak into the answer. Only visible text can complete the stream.
	extractor := &novitaThinkExtractor{}
	receivedAnswer := false
	emit := func(content, reasoning string) error {
		if err := requestContext(apiConfig).Err(); err != nil {
			return err
		}
		if reasoning != "" {
			if err := sender(nil, &reasoning); err != nil {
				return err
			}
		}
		if content != "" {
			if err := sender(&content, nil); err != nil {
				return err
			}
			receivedAnswer = receivedAnswer || strings.TrimSpace(content) != ""
		}
		return nil
	}
	return readLegacyChatStream(resp.Body, apiConfig, false, func(content, reasoning *string) error {
		if reasoning != nil {
			if err := emit("", *reasoning); err != nil {
				return err
			}
		}
		if content == nil {
			return nil
		}
		if *content == "[DONE]" {
			if tail := extractor.Flush(); tail != nil {
				if err := emit(tail.content, tail.reasoning); err != nil {
					return err
				}
			}
			if !receivedAnswer {
				return fmt.Errorf("model stream returned no text answer")
			}
			if err := requestContext(apiConfig).Err(); err != nil {
				return err
			}
			return sender(content, nil)
		}
		for _, segment := range extractor.Feed(*content) {
			if err := emit(segment.content, segment.reasoning); err != nil {
				return err
			}
		}
		return nil
	})
}

// Embed returns indexed embeddings for a list of texts
func (m *GiteeModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(m.httpClient, m.BaseURL, m.URLSuffix.Embedding, modelName, texts, apiConfig, embeddingConfig, embeddingHTTPOptions{fallbackRegion: true})
}

// getMetadata keeps public discovery separate from authenticated account queries.
// Instance endpoint maps are already isolated by the service boundary.
func (m *GiteeModel) getMetadata(suffix, action string, api *APIConfig, authenticated bool) (*http.Response, error) {
	if authenticated && (api.APIKey == nil || strings.TrimSpace(*api.APIKey) == "") {
		return nil, fmt.Errorf("gitee: %s API key is required", action)
	}
	if strings.Trim(suffix, "/ ") == "" {
		return nil, fmt.Errorf("gitee: %s endpoint is missing", action)
	}
	base, err := resolveModelBaseURL(m.BaseURL, api.Region)
	if err != nil {
		return nil, fmt.Errorf("gitee: %s region endpoint is missing", action)
	}
	req, err := http.NewRequestWithContext(requestContext(api), http.MethodGet, joinModelURL(base, suffix), nil)
	if err != nil {
		return nil, fmt.Errorf("gitee: invalid %s endpoint", action)
	}
	req.Header.Set("Accept", "application/json")
	if authenticated {
		req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*api.APIKey))
	}
	client := *m.httpClient
	client.CheckRedirect = rejectModelRedirect
	resp, err := client.Do(req)
	if err != nil {
		if err := requestContext(api).Err(); err != nil {
			return nil, err
		}
		return nil, fmt.Errorf("gitee: %s request failed", action)
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("gitee: %s request failed (HTTP %d)", action, resp.StatusCode)
	}
	return resp, nil
}

func (m *GiteeModel) ListModels(api *APIConfig) ([]string, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := m.getMetadata(m.URLSuffix.Models, "discovery", local, false)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	names, err := strictProviderModelNames(resp.Body)
	if ctxErr := requestContext(local).Err(); ctxErr != nil {
		return nil, ctxErr
	}
	if err != nil {
		return nil, fmt.Errorf("gitee: invalid discovery response")
	}
	return names, nil
}

func (m *GiteeModel) Balance(api *APIConfig) (map[string]interface{}, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := m.getMetadata(m.URLSuffix.Balance, "balance", local, true)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var payload struct {
		Balance *float64        `json:"balance"`
		Error   json.RawMessage `json:"error"`
		Code    json.RawMessage `json:"code"`
	}
	err = decodeBoundedModelJSON(resp.Body, &payload)
	if ctxErr := requestContext(local).Err(); ctxErr != nil {
		return nil, ctxErr
	}
	if err != nil || payload.Balance == nil || len(payload.Error) > 0 && string(payload.Error) != "null" {
		return nil, fmt.Errorf("gitee: invalid balance response")
	}
	if err := validateModelResponseCode(payload.Code); err != nil {
		return nil, fmt.Errorf("gitee: balance provider error")
	}
	return map[string]interface{}{"balance": *payload.Balance, "currency": "CNY"}, nil
}

func (m *GiteeModel) CheckConnection(apiConfig *APIConfig) error {
	apiConfig, cancelCall := modelCallConfig(apiConfig)
	defer cancelCall()
	resp, err := m.getMetadata(m.URLSuffix.Status, "connection", apiConfig, true)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, err = io.Copy(io.Discard, io.LimitReader(resp.Body, 64*1024))
	return err
}

// Rerank returns validated indexed results, ordered by relevance.
func (m *GiteeModel) Rerank(modelName *string, query string, texts []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return rerankHTTP(m.httpClient, m.BaseURL, m.URLSuffix.Rerank, modelName, query, texts, apiConfig, rerankConfig, nil)
}
