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
func (z *ZhipuAIModel) Chat(modelName, message *string, apiConfig *APIConfig, config *ChatConfig) (*ChatResponse, error) {
	if modelName == nil || message == nil {
		return nil, fmt.Errorf("model and message are required")
	}
	return z.ChatWithMessages(*modelName, apiConfig, []Message{{Role: "user", Content: *message}}, config)
}

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
func (z *ZhipuAIModel) ChatWithMessages(modelName string, apiConfig *APIConfig, messages []Message, config *ChatConfig) (*ChatResponse, error) {
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
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
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
	if apiConfig == nil || apiConfig.APIKey == nil || *apiConfig.APIKey == "" {
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
	req.Header.Set("Authorization", "Bearer "+*apiConfig.APIKey)
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

func (z *ZhipuAIModel) ChatStreamlyWithSender(modelName, message *string, apiConfig *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if modelName == nil || message == nil {
		return fmt.Errorf("zhipu-ai: model name and message are required")
	}
	return z.ChatStreamlyWithMessages(*modelName, []Message{{Role: "user", Content: *message}}, apiConfig, config, sender)
}

func (z *ZhipuAIModel) ChatStreamlyWithMessages(name string, messages []Message, apiConfig *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
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

// Encode encodes a list of texts into embeddings
func (z *ZhipuAIModel) Encode(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([][]float64, error) {
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

	if err := ValidateEmbeddings(embeddings, len(texts)); err != nil {
		return nil, err
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

func (m *ZhipuAIModel) Rerank(modelName *string, query string, texts []string, apiConfig *APIConfig) ([]float64, error) {
	return nil, fmt.Errorf("%s: rerank is not supported", m.Name())
}
