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
	"net/http"
	"strings"
	"time"
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

// Chat sends a message and returns response
func (z *ZhipuAIModel) Chat(modelName, message *string, apiConfig *APIConfig, modelConfig *ChatConfig) (*ChatResponse, error) {
	if message == nil {
		return nil, fmt.Errorf("message is nil")
	}
	if apiConfig == nil || apiConfig.APIKey == nil {
		return nil, fmt.Errorf("API key is nil")
	}

	baseURL, err := z.resolveBaseURL(apiConfig.Region)
	if err != nil {
		return nil, err
	}
	url := joinModelURL(baseURL, z.URLSuffix.Chat)

	// Build request body
	reqBody := map[string]interface{}{
		"model": modelName,
		"messages": []map[string]string{
			{"role": "user", "content": *message},
		},
		"stream":      false,
		"temperature": 1,
	}

	if modelConfig != nil {
		if modelConfig.Stream != nil {
			reqBody["stream"] = *modelConfig.Stream
		}
		if modelConfig.MaxTokens != nil {
			reqBody["max_tokens"] = *modelConfig.MaxTokens
		}
		if modelConfig.Temperature != nil {
			reqBody["temperature"] = *modelConfig.Temperature
		}
		if modelConfig.TopP != nil {
			reqBody["top_p"] = *modelConfig.TopP
		}
		if modelConfig.Stop != nil {
			reqBody["stop"] = *modelConfig.Stop
		}
		if modelConfig.Thinking != nil {
			thinkingType := "disabled"
			if *modelConfig.Thinking {
				thinkingType = "enabled"
			}
			reqBody["thinking"] = map[string]interface{}{"type": thinkingType}
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

	resp, err := z.httpClient.Do(req)
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
	if err := json.Unmarshal(body, &result); err != nil {
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

	reasoningContent := ""
	if modelConfig != nil && modelConfig.Thinking != nil && *modelConfig.Thinking {
		var ok bool
		reasoningContent, ok = messageMap["reasoning_content"].(string)
		if !ok {
			return nil, fmt.Errorf("invalid reasoning content format")
		}
		reasoningContent = strings.TrimPrefix(reasoningContent, "\n")
	}

	return &ChatResponse{Answer: &content, ReasoningContent: &reasoningContent}, nil
}

// ChatStreamly sends a message and streams response
func (z *ZhipuAIModel) ChatStreamly(modelName, apiKey, message *string, genConf map[string]interface{}) (<-chan string, error) {
	baseURL, err := z.resolveBaseURL(nil)
	if err != nil {
		return nil, err
	}
	url := joinModelURL(baseURL, z.URLSuffix.Chat)

	// Build request body with streaming enabled
	reqBody := map[string]interface{}{
		"model": modelName,
		"messages": []map[string]string{
			{"role": "user", "content": *message},
		},
		"stream":      true,
		"temperature": 1,
	}

	// Add generation config if provided
	if genConf != nil {
		if maxTokens, ok := genConf["max_tokens"]; ok {
			reqBody["max_tokens"] = maxTokens
		}
		if temperature, ok := genConf["temperature"]; ok {
			reqBody["temperature"] = temperature
		}
		if topP, ok := genConf["top_p"]; ok {
			reqBody["top_p"] = topP
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
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiKey))

	resp, err := z.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		return nil, fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, string(body))
	}

	// Create channel for streaming
	resultChan := make(chan string)

	go func() {
		defer close(resultChan)
		defer resp.Body.Close()

		// SSE parsing: read line by line
		scanner := bufio.NewScanner(resp.Body)
		for scanner.Scan() {
			line := scanner.Text()

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
			if err := json.Unmarshal([]byte(data), &event); err != nil {
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
				resultChan <- content
			}

			finishReason, ok := firstChoice["finish_reason"].(string)
			if ok && finishReason != "" {
				break
			}
		}
	}()

	return resultChan, nil
}

// ChatStreamlyWithChannel sends a message and streams response to channel (better performance)
func (z *ZhipuAIModel) ChatStreamlyWithChannel(modelName, apiKey, message *string, genConf map[string]interface{}, resultChan chan<- string) error {
	baseURL, err := z.resolveBaseURL(nil)
	if err != nil {
		return err
	}
	url := joinModelURL(baseURL, z.URLSuffix.Chat)

	// Build request body with streaming enabled
	reqBody := map[string]interface{}{
		"model": modelName,
		"messages": []map[string]string{
			{"role": "user", "content": *message},
		},
		"stream":      true,
		"temperature": 1,
	}

	// Add generation config if provided
	if genConf != nil {
		if maxTokens, ok := genConf["max_tokens"]; ok {
			reqBody["max_tokens"] = maxTokens
		}
		if temperature, ok := genConf["temperature"]; ok {
			reqBody["temperature"] = temperature
		}
		if topP, ok := genConf["top_p"]; ok {
			reqBody["top_p"] = topP
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
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiKey))

	resp, err := z.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, string(body))
	}

	// SSE parsing: read line by line
	scanner := bufio.NewScanner(resp.Body)
	for scanner.Scan() {
		line := scanner.Text()

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
		if err := json.Unmarshal([]byte(data), &event); err != nil {
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
			resultChan <- content
		}

		finishReason, ok := firstChoice["finish_reason"].(string)
		if ok && finishReason != "" {
			break
		}
	}

	// Send [DONE] marker for OpenAI compatibility
	resultChan <- "[DONE]"

	return scanner.Err()
}

// ChatWithMessages sends multiple messages with roles and returns response
func (z *ZhipuAIModel) ChatWithMessages(modelName string, apiKey *string, messages []Message, chatModelConfig *ChatConfig) (string, error) {
	if apiKey == nil || *apiKey == "" {
		return "", fmt.Errorf("api key is nil or empty")
	}

	if len(messages) == 0 {
		return "", fmt.Errorf("messages is empty")
	}

	url := fmt.Sprintf("%s/%s", z.BaseURL["default"], z.URLSuffix.Chat)

	// Convert messages to the format expected by API
	apiMessages := make([]map[string]string, len(messages))
	for i, msg := range messages {
		apiMessages[i] = map[string]string{
			"role":    msg.Role,
			"content": msg.Content,
		}
	}

	// Build request body
	reqBody := map[string]interface{}{
		"model":       modelName,
		"messages":    apiMessages,
		"stream":      false,
		"temperature": 1,
	}

	if chatModelConfig != nil {
		if chatModelConfig.MaxTokens != nil {
			reqBody["max_tokens"] = *chatModelConfig.MaxTokens
		}

		if chatModelConfig.Temperature != nil {
			reqBody["temperature"] = *chatModelConfig.Temperature
		}

		if chatModelConfig.TopP != nil {
			reqBody["top_p"] = *chatModelConfig.TopP
		}
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return "", fmt.Errorf("failed to marshal request: %w", err)
	}

	req, err := http.NewRequest("POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return "", fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiKey))

	resp, err := z.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, string(body))
	}

	// Parse response
	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		return "", fmt.Errorf("failed to parse response: %w", err)
	}

	choices, ok := result["choices"].([]interface{})
	if !ok || len(choices) == 0 {
		return "", fmt.Errorf("no choices in response")
	}

	firstChoice, ok := choices[0].(map[string]interface{})
	if !ok {
		return "", fmt.Errorf("invalid choice format")
	}

	messageMap, ok := firstChoice["message"].(map[string]interface{})
	if !ok {
		return "", fmt.Errorf("invalid message format")
	}

	content, ok := messageMap["content"].(string)
	if !ok {
		return "", fmt.Errorf("invalid content format")
	}

	return content, nil
}

// ChatStreamlyWithSender sends a message and streams response via sender function (best performance, no channel)
func (z *ZhipuAIModel) ChatStreamlyWithSender(modelName, message *string, apiConfig *APIConfig, modelConfig *ChatConfig, sender func(*string, *string) error) error {
	if apiConfig == nil || apiConfig.APIKey == nil {
		return fmt.Errorf("API key is nil")
	}
	baseURL, err := z.resolveBaseURL(apiConfig.Region)
	if err != nil {
		return err
	}
	url := joinModelURL(baseURL, z.URLSuffix.Chat)

	// Build request body with streaming enabled
	reqBody := map[string]interface{}{
		"model": modelName,
		"messages": []map[string]string{
			{"role": "user", "content": *message},
		},
		"stream":      false,
		"temperature": 1,
	}

	if modelConfig != nil {
		if modelConfig.Stream != nil {
			reqBody["stream"] = *modelConfig.Stream
		}

		if modelConfig.MaxTokens != nil {
			reqBody["max_tokens"] = *modelConfig.MaxTokens
		}

		if modelConfig.Temperature != nil {
			reqBody["temperature"] = *modelConfig.Temperature
		}

		if modelConfig.DoSample != nil {
			reqBody["do_sample"] = *modelConfig.DoSample
		}

		if modelConfig.TopP != nil {
			reqBody["top_p"] = *modelConfig.TopP
		}

		if modelConfig.Stop != nil {
			reqBody["stop"] = *modelConfig.Stop
		}

		if modelConfig.Thinking != nil {
			if *modelConfig.Thinking {
				reqBody["thinking"] = map[string]interface{}{
					"type": "enabled",
				}
			} else {
				reqBody["thinking"] = map[string]interface{}{
					"type": "disabled",
				}
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

	resp, err := z.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, string(body))
	}

	// SSE parsing: read line by line
	scanner := bufio.NewScanner(resp.Body)
	for scanner.Scan() {
		line := scanner.Text()

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
		if err := json.Unmarshal([]byte(data), &event); err != nil {
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
			if err := sender(&content, nil); err != nil {
				return err
			}
		}

		reasoningContent, ok := delta["reasoning_content"].(string)
		if ok && reasoningContent != "" {
			if err := sender(nil, &reasoningContent); err != nil {
				return err
			}
		}

		finishReason, ok := firstChoice["finish_reason"].(string)
		if ok && finishReason != "" {
			break
		}
	}

	// Send [DONE] marker for OpenAI compatibility
	endOfStream := "[DONE]"
	if err := sender(&endOfStream, nil); err != nil {
		return err
	}

	return scanner.Err()
}

// EncodeToEmbedding encodes a list of texts into embeddings
func (z *ZhipuAIModel) EncodeToEmbedding(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([][]float64, error) {
	if apiConfig == nil || apiConfig.APIKey == nil {
		return nil, fmt.Errorf("API key is nil")
	}
	baseURL, err := z.resolveBaseURL(apiConfig.Region)
	if err != nil {
		return nil, err
	}
	url := joinModelURL(baseURL, z.URLSuffix.Embedding)

	embeddings := make([][]float64, len(texts))

	for i, text := range texts {
		reqBody := map[string]interface{}{
			"model": modelName,
			"input": text,
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

		resp, err := z.httpClient.Do(req)
		if err != nil {
			return nil, fmt.Errorf("failed to send request: %w", err)
		}

		body, err := io.ReadAll(resp.Body)
		resp.Body.Close()

		if err != nil {
			return nil, fmt.Errorf("failed to read response: %w", err)
		}

		if resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, string(body))
		}

		// Parse response
		var result map[string]interface{}
		if err := json.Unmarshal(body, &result); err != nil {
			return nil, fmt.Errorf("failed to parse response: %w", err)
		}

		data, ok := result["data"].([]interface{})
		if !ok || len(data) == 0 {
			return nil, fmt.Errorf("no data in response")
		}

		firstData, ok := data[0].(map[string]interface{})
		if !ok {
			return nil, fmt.Errorf("invalid data format")
		}

		embeddingSlice, ok := firstData["embedding"].([]interface{})
		if !ok {
			return nil, fmt.Errorf("invalid embedding format")
		}

		embedding := make([]float64, len(embeddingSlice))
		for j, v := range embeddingSlice {
			switch val := v.(type) {
			case float64:
				embedding[j] = val
			case float32:
				embedding[j] = float64(val)
			default:
				return nil, fmt.Errorf("unexpected embedding value type")
			}
		}

		embeddings[i] = embedding
	}

	return embeddings, nil
}

func (z *ZhipuAIModel) ListModels(apiConfig *APIConfig) ([]string, error) {
	return nil, fmt.Errorf("listing supported models is not available for Zhipu AI")
}

func (z *ZhipuAIModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("balance query is not available for Zhipu AI")
}

func (z *ZhipuAIModel) CheckConnection(apiConfig *APIConfig) error {
	return checkBearerEndpointConnection(z.httpClient, z.BaseURL, z.URLSuffix.Files, apiConfig, z.Name())
}
