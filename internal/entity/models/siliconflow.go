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
	"multirag/internal/logger"
	"net/http"
	"strings"
	"time"
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
		BaseURL:   baseURL,
		URLSuffix: urlSuffix,
		httpClient: &http.Client{
			Timeout: 120 * time.Second,
			Transport: &http.Transport{
				MaxIdleConns:        100,
				MaxIdleConnsPerHost: 10,
				IdleConnTimeout:     90 * time.Second,
				DisableCompression:  false,
			},
		},
	}
}

func (m *SiliconFlowModel) Name() string {
	return "siliconflow"
}

// Chat sends a message and returns response
func (m *SiliconFlowModel) Chat(modelName, message *string, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	if modelName == nil {
		return nil, fmt.Errorf("model name is nil")
	}
	if message == nil {
		return nil, fmt.Errorf("message is nil")
	}
	if apiConfig == nil || apiConfig.APIKey == nil {
		return nil, fmt.Errorf("API key is nil")
	}
	chatModelConfig = normalizeChatConfig(chatModelConfig)

	var region = "default"
	if apiConfig.Region != nil {
		region = *apiConfig.Region
	}

	url := fmt.Sprintf("%s/%s", m.BaseURL[region], m.URLSuffix.Chat)

	modelClass := chatModelClass(*modelName, chatModelConfig)
	if (modelClass == "qwen" || modelClass == "glm") && m.URLSuffix.AsyncChat != "" {
		url = fmt.Sprintf("%s/%s", m.BaseURL[region], m.URLSuffix.AsyncChat)
	}

	// Build request body
	reqBody := map[string]interface{}{
		"model": modelName,
		"messages": []map[string]string{
			{"role": "user", "content": *message},
		},
		"stream":      false,
		"temperature": 1,
	}

	if chatModelConfig.Stream != nil {
		reqBody["stream"] = *chatModelConfig.Stream
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

	req, err := http.NewRequest("POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.APIKey))

	resp, err := m.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, string(body))
	}

	// Parse response
	var result map[string]interface{}
	if err = json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	choices, ok := result["choices"].([]interface{})
	if !ok || len(choices) == 0 {
		return nil, fmt.Errorf("no choices in response")
	}

	firstChoice, ok := choices[0].(map[string]interface{})
	if !ok {
		return nil, fmt.Errorf("invalid choice format")
	}

	messageMap, ok := firstChoice["message"].(map[string]interface{})
	if !ok {
		return nil, fmt.Errorf("invalid message format")
	}

	content, ok := messageMap["content"].(string)
	if !ok {
		return nil, fmt.Errorf("invalid content format")
	}

	thinking, answer := GetThinkingAndAnswer(&modelClass, &content)

	chatResponse := &ChatResponse{
		Answer:           answer,
		ReasoningContent: thinking,
	}

	return chatResponse, nil
}

// ChatWithMessages sends multiple messages with roles and returns response
func (m *SiliconFlowModel) ChatWithMessages(modelName string, apiConfig *APIConfig, messages []Message, chatModelConfig *ChatConfig) (string, error) {
	return "", fmt.Errorf("%s, ChatWithMessages not implemented", m.Name())
}

func (m *SiliconFlowModel) ChatStreamly(modelName, apiKey, message *string, genConf map[string]interface{}) (<-chan string, error) {
	return nil, fmt.Errorf("streaming chat is not implemented for %s", m.Name())
}

func (m *SiliconFlowModel) ChatStreamlyWithChannel(modelName, apiKey, message *string, genConf map[string]interface{}, resultChan chan<- string) error {
	return fmt.Errorf("streaming chat is not implemented for %s", m.Name())
}

// ChatStreamlyWithSender sends a message and streams response via sender function (best performance, no channel)
func (m *SiliconFlowModel) ChatStreamlyWithSender(modelName, message *string, apiConfig *APIConfig, chatModelConfig *ChatConfig, sender func(*string, *string) error) error {
	if message == nil || apiConfig == nil || apiConfig.APIKey == nil {
		return fmt.Errorf("message or API key is nil")
	}
	chatModelConfig = normalizeChatConfig(chatModelConfig)
	var region = "default"
	if apiConfig.Region != nil {
		region = *apiConfig.Region
	}

	url := fmt.Sprintf("%s/chat/completions", m.BaseURL[region])

	// Build request body with streaming enabled
	reqBody := map[string]interface{}{
		"model": modelName,
		"messages": []map[string]string{
			{"role": "user", "content": *message},
		},
		"stream":      false,
		"temperature": 1,
	}

	if chatModelConfig.Stream != nil {
		reqBody["stream"] = *chatModelConfig.Stream
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

	req, err := http.NewRequest("POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.APIKey))

	resp, err := m.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, string(body))
	}

	reserveText := ""
	thinkingPhase := false
	answerPhase := false

	// SSE parsing: read line by line
	scanner := bufio.NewScanner(resp.Body)
	for scanner.Scan() {
		line := scanner.Text()
		logger.Info(line)

		// SSE data line starts with "data:"
		if !strings.HasPrefix(line, "data:") {
			continue
		}

		// Extract JSON after "data:"
		data := strings.TrimSpace(line[5:])

		// [DONE] marks the end of stream
		if data == "[DONE]" {
			break
		}

		// Parse the JSON event
		var event map[string]interface{}
		if err = json.Unmarshal([]byte(data), &event); err != nil {
			continue
		}

		choices, ok := event["choices"].([]interface{})
		if !ok || len(choices) == 0 {
			continue
		}

		firstChoice, ok := choices[0].(map[string]interface{})
		if !ok {
			continue
		}

		delta, ok := firstChoice["delta"].(map[string]interface{})
		if !ok {
			continue
		}

		content, ok := delta["content"].(string)
		if ok && content != "" {
			if content == "<think>" {
				thinkingPhase = true
				continue

			} else if content == "</think>" {
				thinkingPhase = false
				answerPhase = true
				continue
			}

			if thinkingPhase {
				if err = sender(nil, &content); err != nil {
					return err
				}
				reserveText = ""
			} else if answerPhase {
				if err = sender(&content, nil); err != nil {
					return err
				}
				reserveText = ""
			} else {
				content = strings.Trim(content, "\n")
				content = strings.Trim(content, " ")
				if content != "" {
					reserveText += content
				}
			}
		}

		finishReason, ok := firstChoice["finish_reason"].(string)
		if ok && finishReason != "" {
			break
		}
	}

	if reserveText != "" {
		if err = sender(&reserveText, nil); err != nil {
			return err
		}
	}

	// Send [DONE] marker for OpenAI compatibility
	endOfStream := "[DONE]"
	if err = sender(&endOfStream, nil); err != nil {
		return err
	}

	return scanner.Err()
}

// Encode encodes a list of texts into embeddings
func (m *SiliconFlowModel) Encode(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([][]float64, error) {
	return encodeHTTP(m.httpClient, m.BaseURL, m.URLSuffix.Embedding, modelName, texts, apiConfig)
}

func (m *SiliconFlowModel) ListModels(apiConfig *APIConfig) ([]string, error) {
	var region = "default"
	if apiConfig.Region != nil {
		region = *apiConfig.Region
	}

	url := fmt.Sprintf("%s/%s", m.BaseURL[region], m.URLSuffix.Models)

	// Build request body
	reqBody := map[string]interface{}{}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	req, err := http.NewRequest("GET", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.APIKey))

	resp, err := m.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, string(body))
	}

	// Parse response
	var modelList providerModelList
	if err = json.Unmarshal(body, &modelList); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	var models []string
	for _, model := range modelList.Models {
		modelName := model.ID
		if model.OwnedBy != "" {
			modelName = model.ID + "@" + model.OwnedBy
		}
		models = append(models, modelName)
	}

	return models, nil
}

func (m *SiliconFlowModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("%s, no such method", m.Name())
}

func (m *SiliconFlowModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := m.ListModels(apiConfig)
	if err != nil {
		return err
	}
	return nil
}

func (m *SiliconFlowModel) Rerank(name *string, query string, texts []string, config *APIConfig) ([]float64, error) {
	if len(texts) == 0 {
		return []float64{}, nil
	}
	if name == nil || *name == "" || config == nil || config.APIKey == nil || *config.APIKey == "" {
		return nil, fmt.Errorf("rerank model name and API key are required")
	}
	if m.URLSuffix.Rerank == "" {
		return nil, fmt.Errorf("rerank endpoint is not configured")
	}
	baseURL, err := resolveModelBaseURL(m.BaseURL, config.Region)
	if err != nil {
		return nil, err
	}
	body, err := json.Marshal(map[string]interface{}{"model": *name, "query": query, "documents": texts, "top_n": len(texts), "return_documents": false, "max_chunks_per_doc": 1024, "overlap_tokens": 80})
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(requestContext(config), http.MethodPost, joinModelURL(baseURL, m.URLSuffix.Rerank), bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+*config.APIKey)
	resp, err := m.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("rerank request failed (HTTP %d)", resp.StatusCode)
	}
	var result struct {
		Results []struct {
			Index *int     `json:"index"`
			Score *float64 `json:"relevance_score"`
		} `json:"results"`
		Error json.RawMessage `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, err
	}
	if len(result.Error) != 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("rerank provider returned an error")
	}
	if len(result.Results) != len(texts) {
		return nil, fmt.Errorf("rerank result count mismatch")
	}
	scores := make([]float64, len(texts))
	seen := make([]bool, len(texts))
	for _, item := range result.Results {
		if item.Index == nil || *item.Index < 0 || *item.Index >= len(texts) || seen[*item.Index] || item.Score == nil {
			return nil, fmt.Errorf("invalid rerank result")
		}
		seen[*item.Index], scores[*item.Index] = true, *item.Score
	}
	return scores, nil
}

func (m *SiliconFlowModel) ChatStreamlyWithMessages(modelName string, messages []Message, apiConfig *APIConfig, modelConfig *ChatConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s: history streaming is unsupported", m.Name())
}
