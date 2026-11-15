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
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"strings"
)

// ZhipuAIModel implements ModelDriver for Zhipu AI
type ZhipuAIModel struct {
	baseModel BaseModel
}

// NewZhipuAIModel creates a new Zhipu AI model instance
func NewZhipuAIModel(baseURL map[string]string, urlSuffix URLSuffix) *ZhipuAIModel {
	if baseURL != nil {
		copied := make(map[string]string, len(baseURL))
		for region, value := range baseURL {
			copied[region] = value
		}
		baseURL = copied
	}
	return &ZhipuAIModel{
		baseModel: BaseModel{
			BaseURL:    baseURL,
			URLSuffix:  urlSuffix,
			httpClient: NewDriverHTTPClient(),
		},
	}
}

func (z *ZhipuAIModel) NewInstance(baseURL map[string]string) ModelDriver {
	return NewZhipuAIModel(baseURL, z.baseModel.URLSuffix)
}

func (z *ZhipuAIModel) Name() string {
	return "zhipu"
}

// ChatWithMessages sends multiple messages with roles and returns response
func (z *ZhipuAIModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	if err := z.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(messages) == 0 {
		return nil, fmt.Errorf("messages is empty")
	}

	baseURL, err := z.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}

	url := fmt.Sprintf("%s/%s", baseURL, z.baseModel.URLSuffix.Chat)

	// Convert messages to the format expected by API
	apiMessages := make([]map[string]interface{}, len(messages))
	for i, msg := range messages {
		apiMessages[i] = map[string]interface{}{
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
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := z.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	// Parse response
	var result map[string]interface{}

	var responseEnvelope map[string]json.RawMessage
	if json.Unmarshal(body, &responseEnvelope) != nil || responseEnvelope == nil {
		return nil, fmt.Errorf("invalid chat response")
	}
	if value := responseEnvelope["error"]; value != nil && string(value) != "null" {
		return nil, fmt.Errorf("upstream chat error")
	}
	if value := responseEnvelope["code"]; value != nil {
		var code float64
		if json.Unmarshal(value, &code) == nil && code != 0 {
			return nil, fmt.Errorf("upstream chat error")
		}
	}
	var responseChoices []map[string]json.RawMessage
	if json.Unmarshal(responseEnvelope["choices"], &responseChoices) != nil || len(responseChoices) == 0 || responseChoices[0] == nil {
		return nil, fmt.Errorf("invalid chat choices")
	}
	var responseMessage map[string]json.RawMessage
	if json.Unmarshal(responseChoices[0]["message"], &responseMessage) != nil || responseMessage == nil {
		return nil, fmt.Errorf("invalid chat message")
	}
	var responseContent string
	if json.Unmarshal(responseMessage["content"], &responseContent) != nil || strings.TrimSpace(responseContent) == "" {
		return nil, fmt.Errorf("invalid chat content")
	}
	for _, field := range []string{"reasoning_content", "reasoning"} {
		if value := responseMessage[field]; value != nil && string(value) != "null" {
			var text string
			if json.Unmarshal(value, &text) != nil {
				return nil, fmt.Errorf("invalid reasoning content")
			}
		}
	}
	if value := responseChoices[0]["finish_reason"]; value != nil && string(value) != "null" {
		var finish string
		if json.Unmarshal(value, &finish) != nil || (finish != "" && finish != "stop" && finish != "length") {
			return nil, fmt.Errorf("unsuccessful chat finish reason")
		}
	}
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

	var reasonContent string
	if chatModelConfig != nil && chatModelConfig.Thinking != nil && *chatModelConfig.Thinking {
		reasonContent, ok = messageMap["reasoning_content"].(string)
		if !ok {
			return nil, fmt.Errorf("invalid content format")
		}
		// if first char of reasonContent is \n remove the '\n'
		if reasonContent != "" && reasonContent[0] == '\n' {
			reasonContent = reasonContent[1:]
		}
	}

	chatResponse := &ChatResponse{
		Answer:        &content,
		ReasonContent: &reasonContent,
	}

	return chatResponse, nil
}

// ChatStreamlyWithSender sends messages and streams response via sender function (best performance, no channel)
func (z *ZhipuAIModel) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	if chatModelConfig == nil {
		chatModelConfig = &ChatConfig{}
	}

	if err := z.baseModel.APIConfigCheck(apiConfig); err != nil {
		return err
	}

	if len(messages) == 0 {
		return fmt.Errorf("messages is empty")
	}

	baseURL, err := z.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return err
	}

	url := fmt.Sprintf("%s/%s", baseURL, z.baseModel.URLSuffix.Chat)

	// Convert messages to API format
	apiMessages := make([]map[string]interface{}, len(messages))
	for i, msg := range messages {
		apiMessages[i] = map[string]interface{}{
			"role":    msg.Role,
			"content": msg.Content,
		}
	}

	// Build request body with streaming enabled
	reqBody := map[string]interface{}{
		"model":       modelName,
		"messages":    apiMessages,
		"stream":      true,
		"temperature": 1,
	}

	if chatModelConfig != nil {
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
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return fmt.Errorf("failed to marshal request: %w", err)
	}

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), streamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := z.baseModel.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 20*1024*1024))
		return fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	// SSE parsing: read line by line
	responseHasContent, responseHasTerminal := false, false
	responseDone, responseError := ParseSSEStream[map[string]interface{}](resp.Body, func(rawEvent map[string]interface{}) error {
		if err := apiConfig.RequestContext().Err(); err != nil {
			return err
		}
		if rawEvent == nil || rawEvent["error"] != nil {
			return fmt.Errorf("upstream stream error")
		}
		if code, ok := rawEvent["code"].(float64); ok && code != 0 {
			return fmt.Errorf("upstream stream error")
		}
		terminal := false
		validateFinish := func(value interface{}) error {
			if value == nil {
				return nil
			}
			finish, ok := value.(string)
			if !ok {
				return fmt.Errorf("invalid finish reason")
			}
			if finish != "" {
				if finish != "stop" && finish != "length" {
					return fmt.Errorf("unsuccessful finish reason")
				}
				terminal = true
			}
			return nil
		}
		if err := validateFinish(rawEvent["finish_reason"]); err != nil {
			return err
		}
		rawChoices, ok := rawEvent["choices"].([]interface{})
		if !ok || len(rawChoices) == 0 {
			if terminal {
				responseHasTerminal = true
				return nil
			}
			if ok && rawEvent["usage"] != nil {
				return nil
			}
			return fmt.Errorf("invalid stream choices")
		}
		rawChoice, ok := rawChoices[0].(map[string]interface{})
		if !ok {
			return fmt.Errorf("invalid stream choice")
		}
		if err := validateFinish(rawChoice["finish_reason"]); err != nil {
			return err
		}
		rawDelta, ok := rawChoice["delta"].(map[string]interface{})
		if !ok {
			if !terminal || rawChoice["delta"] != nil {
				return fmt.Errorf("invalid stream delta")
			}
			rawDelta = map[string]interface{}{}
			rawChoice["delta"] = rawDelta
		}
		for _, field := range []string{"content", "reasoning_content", "reasoning"} {
			if value, exists := rawDelta[field]; exists && value != nil {
				text, ok := value.(string)
				if !ok {
					return fmt.Errorf("invalid stream text")
				}
				if strings.TrimSpace(text) != "" {
					responseHasContent = true
				}
			}
		}
		if terminal {
			responseHasTerminal = true
		}

		event := rawEvent

		choices, ok := event["choices"].([]interface{})
		if !ok || len(choices) == 0 {
			return nil
		}

		firstChoice, ok := choices[0].(map[string]interface{})
		if !ok {
			return nil
		}

		delta, ok := firstChoice["delta"].(map[string]interface{})
		if !ok {
			return nil
		}

		reasoningContent, ok := delta["reasoning_content"].(string)
		if ok && reasoningContent != "" {
			if err := sender(nil, &reasoningContent); err != nil {
				return err
			}
			if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
				return parentErr
			}

		}

		content, ok := delta["content"].(string)
		if ok && content != "" {
			if err := sender(&content, nil); err != nil {
				return err
			}
		}

		return nil
	})
	if responseError != nil {
		if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
			return parentErr
		}
		err := responseError

		return fmt.Errorf("failed to scan response body: %w", err)

	}

	// Send [DONE] marker for OpenAI compatibility
	if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
		return parentErr
	}
	if !responseHasContent || (!responseDone && !responseHasTerminal) {
		return fmt.Errorf("stream ended before successful content completion")
	}

	endOfStream := "[DONE]"
	if err = sender(&endOfStream, nil); err != nil {
		return err
	}
	if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
		return parentErr
	}

	return nil
}

type zhipuEmbeddingResponse struct {
	Data   []zhipuEmbeddingData `json:"data"`
	Model  string               `json:"model"`
	Object string               `json:"object"`
	Usage  zhipuUsage           `json:"usage"`
}

type zhipuEmbeddingData struct {
	Embedding []float64 `json:"embedding"`
	Index     int       `json:"index"`
	Object    string    `json:"object"`
}

type zhipuUsage struct {
	CompletionTokens int `json:"completion_tokens"`
	PromptTokens     int `json:"prompt_tokens"`
	TotalTokens      int `json:"total_tokens"`
}

// Encode encodes a list of texts into embeddings
func (z *ZhipuAIModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	if len(texts) > 0 && (modelName == nil || strings.TrimSpace(*modelName) == "") {
		return nil, fmt.Errorf("model name is required")
	}

	if embeddingConfig != nil && embeddingConfig.Dimension < 0 {
		return nil, fmt.Errorf("embedding dimension must be nonnegative")
	}

	if err := z.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(texts) == 0 {
		return []EmbeddingData{}, nil
	}

	if modelName == nil || *modelName == "" {
		return nil, fmt.Errorf("model name is required")
	}

	baseURL, err := z.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}

	url := fmt.Sprintf("%s/%s", baseURL, z.baseModel.URLSuffix.Embedding)

	reqBody := map[string]interface{}{}
	reqBody["model"] = modelName
	reqBody["input"] = texts
	if embeddingConfig != nil && embeddingConfig.Dimension > 0 {
		reqBody["dimensions"] = embeddingConfig.Dimension
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := z.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}

	body, err := io.ReadAll(resp.Body)
	resp.Body.Close()

	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	// Parse response
	var envelope map[string]json.RawMessage
	if json.Unmarshal(body, &envelope) != nil || envelope == nil {
		return nil, fmt.Errorf("invalid embedding response")
	}
	if value := envelope["error"]; value != nil && string(value) != "null" {
		return nil, fmt.Errorf("upstream embedding error")
	}
	var numeric []EmbeddingData
	if json.Unmarshal(envelope["data"], &numeric) != nil || len(numeric) != len(texts) {
		return nil, fmt.Errorf("invalid embedding count or numeric values")
	}
	seenNumeric := make(map[int]bool, len(numeric))
	dimension := 0
	for _, row := range numeric {
		if row.Index < 0 || row.Index >= len(texts) || seenNumeric[row.Index] {
			return nil, fmt.Errorf("invalid embedding identity")
		}
		seenNumeric[row.Index] = true
		if dimension == 0 {
			dimension = len(row.Embedding)
		} else if dimension != len(row.Embedding) {
			return nil, fmt.Errorf("inconsistent embedding dimensions")
		}
		if embeddingConfig != nil && embeddingConfig.Dimension > 0 && len(row.Embedding) != embeddingConfig.Dimension {
			return nil, fmt.Errorf("unexpected embedding dimension")
		}
	}

	var zhipuResp zhipuEmbeddingResponse
	if err = json.Unmarshal(body, &zhipuResp); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	var embeddings []EmbeddingData
	for _, dataElem := range zhipuResp.Data {
		var embeddingData EmbeddingData
		embeddingData.Embedding = dataElem.Embedding
		embeddingData.Index = dataElem.Index
		embeddings = append(embeddings, embeddingData)
	}

	return embeddings, nil
}

func (z *ZhipuAIModel) ListModels(apiConfig *APIConfig) ([]ListModelResponse, error) {
	if err := z.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	baseURL, err := z.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}

	url := fmt.Sprintf("%s/%s", baseURL, z.baseModel.URLSuffix.Models)

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := z.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("ZhipuAI models API error: %s, body: %s", resp.Status, "[redacted]")
	}

	// Parse response
	var envelope map[string]json.RawMessage
	if json.Unmarshal(body, &envelope) != nil || envelope == nil || (envelope["error"] != nil && string(envelope["error"]) != "null") {
		return nil, fmt.Errorf("invalid model discovery response")
	}
	var modelList ModelList
	if err = json.Unmarshal(body, &modelList); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	return ParseListModel(modelList), nil
}

func (z *ZhipuAIModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("%s, no such method", z.Name())
}

func (z *ZhipuAIModel) CheckConnection(apiConfig *APIConfig) error {
	if err := z.baseModel.APIConfigCheck(apiConfig); err != nil {
		return err
	}

	baseURL, err := z.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return err
	}

	url := fmt.Sprintf("%s/%s", baseURL, z.baseModel.URLSuffix.Files)

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := z.baseModel.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	_, err = io.Copy(io.Discard, io.LimitReader(resp.Body, 20*1024*1024))
	if err != nil {
		return fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	return nil
}

// zhipuRerankRequest is the request body for the ZhipuAI rerank
// endpoint. The shape matches the standard OpenAI-compatible rerank
// API also used by SiliconFlow.
type zhipuRerankRequest struct {
	Model           string   `json:"model"`
	Query           string   `json:"query"`
	Documents       []string `json:"documents"`
	TopN            int      `json:"top_n"`
	ReturnDocuments bool     `json:"return_documents"`
}

// zhipuRerankResponse is the response shape for the ZhipuAI rerank
// endpoint.
type zhipuRerankResponse struct {
	Created   int64  `json:"created"`
	ID        string `json:"id"`
	RequestID string `json:"request_id"`
	Usage     struct {
		CompletionTokens int `json:"completion_tokens"`
		PromptTokens     int `json:"prompt_tokens"`
		TotalTokens      int `json:"total_tokens"`
	} `json:"usage"`
	Results []struct {
		Index          int     `json:"index"`
		RelevanceScore float64 `json:"relevance_score"`
	} `json:"results"`
}

type zhipuOCRResponse struct {
	MarkdownResults *string `json:"md_results"`
}

// Rerank calculates similarity scores between query and documents using
// the ZhipuAI /rerank endpoint (e.g. glm-rerank). The result is one
// score per input text, in the same order the documents were given.
func (z *ZhipuAIModel) Rerank(modelName *string, query string, documents []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	if len(documents) > 0 && (modelName == nil || strings.TrimSpace(*modelName) == "") {
		return nil, fmt.Errorf("model name is required")
	}

	if rerankConfig == nil {
		rerankConfig = &RerankConfig{}
	}
	if rerankConfig.TopN < 0 {
		return nil, fmt.Errorf("top_n must be nonnegative")
	}
	if len(documents) > 0 && (modelName == nil || strings.TrimSpace(*modelName) == "") {
		return nil, fmt.Errorf("model name is required")
	}

	if err := z.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(documents) == 0 {
		return &RerankResponse{}, nil
	}

	if modelName == nil || *modelName == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if strings.TrimSpace(z.baseModel.URLSuffix.Rerank) == "" {
		return nil, fmt.Errorf("rerank endpoint is required")
	}
	baseURL, err := z.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}

	url := fmt.Sprintf("%s/%s", baseURL, z.baseModel.URLSuffix.Rerank)

	var topN = rerankConfig.TopN
	if rerankConfig.TopN == 0 {
		topN = len(documents)
	}

	reqBody := zhipuRerankRequest{
		Model:           *modelName,
		Query:           query,
		Documents:       documents,
		TopN:            topN,
		ReturnDocuments: false,
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := z.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("ZhipuAI rerank API error: %s, body: %s", resp.Status, "[redacted]")
	}

	var zhipuRerankResp zhipuRerankResponse

	var responseEnvelope map[string]json.RawMessage
	if json.Unmarshal(body, &responseEnvelope) != nil || responseEnvelope == nil || responseEnvelope["error"] != nil {
		return nil, fmt.Errorf("invalid rerank response")
	}

	var numericEnvelope struct {
		Results []RerankResult `json:"results"`
	}
	expectedResults := len(documents)
	if rerankConfig != nil && rerankConfig.TopN > 0 && rerankConfig.TopN < expectedResults {
		expectedResults = rerankConfig.TopN
	}
	if json.Unmarshal(body, &numericEnvelope) != nil || len(numericEnvelope.Results) != expectedResults {
		return nil, fmt.Errorf("invalid rerank count or values")
	}
	seenNumeric := map[int]bool{}
	for _, row := range numericEnvelope.Results {
		if row.Index < 0 || row.Index >= len(documents) || seenNumeric[row.Index] {
			return nil, fmt.Errorf("invalid rerank identity")
		}
		seenNumeric[row.Index] = true
	}
	if err = json.Unmarshal(body, &zhipuRerankResp); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	var rerankResponse RerankResponse
	for _, result := range zhipuRerankResp.Results {
		rerankResult := RerankResult{
			Index:          result.Index,
			RelevanceScore: result.RelevanceScore,
		}
		rerankResponse.Data = append(rerankResponse.Data, rerankResult)
	}

	return &rerankResponse, nil
}

// TranscribeAudio transcribe audio
func (z *ZhipuAIModel) TranscribeAudio(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig) (*ASRResponse, error) {
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if asrConfig != nil {
		for key := range asrConfig.Params {
			switch key {
			case "model", "file", "stream", "api_key":
				return nil, fmt.Errorf("reserved ASR parameter")
			}
		}
	}

	if err := z.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if modelName == nil || *modelName == "" {
		return nil, fmt.Errorf("model name is required")
	}
	if file == nil || *file == "" {
		return nil, fmt.Errorf("file is required")
	}
	if z.baseModel.URLSuffix.ASR == "" {
		return nil, fmt.Errorf("zhipu-ai: ASR URL suffix is not configured")
	}

	baseURL, err := z.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}

	url := fmt.Sprintf("%s/%s", baseURL, strings.TrimLeft(z.baseModel.URLSuffix.ASR, "/"))

	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	if err := writer.WriteField("model", *modelName); err != nil {
		return nil, fmt.Errorf("failed to write model field: %w", err)
	}
	if err := writer.WriteField("stream", "false"); err != nil {
		return nil, fmt.Errorf("failed to write stream field: %w", err)
	}
	if err := writeZhipuASRParams(writer, asrConfig); err != nil {
		return nil, err
	}

	audioFile, err := os.Open(*file)
	if err != nil {
		return nil, fmt.Errorf("failed to open audio file: %w", err)
	}
	defer audioFile.Close()

	part, err := writer.CreateFormFile("file", filepath.Base(*file))
	if err != nil {
		return nil, fmt.Errorf("failed to create multipart file: %w", err)
	}
	if _, err = io.Copy(part, audioFile); err != nil {
		return nil, fmt.Errorf("failed to copy audio data: %w", err)
	}
	if err = writer.Close(); err != nil {
		return nil, fmt.Errorf("failed to close multipart writer: %w", err)
	}

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), longOpCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, &body)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))
	req.Header.Set("Content-Type", writer.FormDataContentType())

	resp, err := z.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("ZhipuAI ASR API error: %s, body: %s", resp.Status, "[redacted]")
	}

	var envelope map[string]json.RawMessage
	if json.Unmarshal(respBody, &envelope) != nil || envelope == nil || envelope["error"] != nil {
		return nil, fmt.Errorf("invalid ASR response")
	}
	if code := envelope["code"]; code != nil {
		var value float64
		if json.Unmarshal(code, &value) == nil && value != 0 {
			return nil, fmt.Errorf("upstream ASR error")
		}
	}
	if value := envelope["text"]; value == nil || string(value) == "null" {
		return nil, fmt.Errorf("missing ASR text")
	}

	var result struct {
		Text string `json:"text"`
	}
	if err = json.Unmarshal(respBody, &result); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}
	return &ASRResponse{Text: result.Text}, nil
}

func writeZhipuASRParams(writer *multipart.Writer, asrConfig *ASRConfig) error {
	if asrConfig == nil || asrConfig.Params == nil {
		return nil
	}
	for key, value := range asrConfig.Params {
		switch key {
		case "model", "stream", "file", "file_base64":
			continue
		}
		if err := writeZhipuASRField(writer, key, value); err != nil {
			return err
		}
	}
	return nil
}

func writeZhipuASRField(writer *multipart.Writer, key string, value interface{}) error {
	switch v := value.(type) {
	case nil:
		return nil
	case []string:
		for _, item := range v {
			if err := writer.WriteField(key, item); err != nil {
				return fmt.Errorf("failed to write field %s: %w", key, err)
			}
		}
		return nil
	case []interface{}:
		for _, item := range v {
			if err := writer.WriteField(key, fmt.Sprint(item)); err != nil {
				return fmt.Errorf("failed to write field %s: %w", key, err)
			}
		}
		return nil
	default:
		if err := writer.WriteField(key, fmt.Sprint(v)); err != nil {
			return fmt.Errorf("failed to write field %s: %w", key, err)
		}
		return nil
	}
}

func (z *ZhipuAIModel) TranscribeAudioWithSender(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", z.Name())
}

// AudioSpeech convert text to audio
func (z *ZhipuAIModel) AudioSpeech(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig) (*TTSResponse, error) {
	if err := z.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	reqBody, url, err := z.buildTTSRequest(modelName, audioContent, apiConfig, ttsConfig, false)
	if err != nil {
		return nil, err
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), longOpCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := z.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("ZhipuAI TTS API error: %s, body: %s", resp.Status, "[redacted]")
	}

	responseType := strings.ToLower(strings.Split(resp.Header.Get("Content-Type"), ";")[0])
	if len(body) == 0 || strings.Contains(responseType, "json") {
		return nil, fmt.Errorf("invalid speech response")
	}
	if ttsConfig != nil && ttsConfig.Format != "" && strings.HasPrefix(responseType, "audio/") {
		expected := strings.ToLower(ttsConfig.Format)
		if (expected == "wav" && responseType != "audio/wav" && responseType != "audio/x-wav") || (expected == "mp3" && responseType != "audio/mpeg" && responseType != "audio/mp3") {
			return nil, fmt.Errorf("speech response format mismatch")
		}
	}

	return &TTSResponse{Audio: body}, nil
}

func (z *ZhipuAIModel) AudioSpeechWithSender(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	if err := z.baseModel.APIConfigCheck(apiConfig); err != nil {
		return err
	}

	if sender == nil {
		return fmt.Errorf("sender is required")
	}
	reqBody, url, err := z.buildTTSRequest(modelName, audioContent, apiConfig, ttsConfig, true)
	if err != nil {
		return err
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return fmt.Errorf("failed to marshal request: %w", err)
	}

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), streamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewBuffer(jsonData))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := z.baseModel.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 20*1024*1024))
		return fmt.Errorf("ZhipuAI stream TTS API error: %s, body: %s", resp.Status, "[redacted]")
	}

	buf := make([]byte, 32*1024)
	for {
		n, err := resp.Body.Read(buf)
		if n > 0 {
			chunk := string(buf[:n])
			if errSend := sender(&chunk, nil); errSend != nil {
				return errSend
			}
			if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
				return parentErr
			}
		}
		if err != nil {
			if err == io.EOF {
				break
			}
			return fmt.Errorf("error reading ZhipuAI binary audio stream: %w", err)
		}
	}
	return nil
}

func (z *ZhipuAIModel) buildTTSRequest(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, stream bool) (map[string]interface{}, string, error) {
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, "", fmt.Errorf("model name is required")
	}

	if modelName == nil || *modelName == "" {
		return nil, "", fmt.Errorf("model name is required")
	}
	if audioContent == nil || *audioContent == "" {
		return nil, "", fmt.Errorf("audio content is empty")
	}
	if z.baseModel.URLSuffix.TTS == "" {
		return nil, "", fmt.Errorf("zhipu-ai: TTS URL suffix is not configured")
	}

	baseURL, err := z.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, "", err
	}

	reqBody := map[string]interface{}{
		"model":  *modelName,
		"input":  *audioContent,
		"stream": stream,
	}
	if ttsConfig != nil {
		for key, value := range ttsConfig.Params {
			switch key {
			case "model", "input", "stream", "response_format":
				continue
			}
			reqBody[key] = value
		}
		if ttsConfig.Format != "" {
			reqBody["response_format"] = ttsConfig.Format
		}
	}

	url := fmt.Sprintf("%s/%s", baseURL, strings.TrimLeft(z.baseModel.URLSuffix.TTS, "/"))
	return reqBody, url, nil
}

// OCRFile OCR file
func (z *ZhipuAIModel) OCRFile(modelName *string, content []byte, fileURL *string, apiConfig *APIConfig, ocrConfig *OCRConfig) (*OCRFileResponse, error) {
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if err := z.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if (fileURL == nil || *fileURL == "") && len(content) == 0 {
		return nil, fmt.Errorf("file url or content is required")
	}

	baseURL, err := z.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}

	if z.baseModel.URLSuffix.OCR == "" {
		return nil, fmt.Errorf("zhipu-ai: no OCR URL suffix configured")
	}

	file := ""
	if fileURL != nil && *fileURL != "" {
		file = *fileURL
	} else {
		mimeType := http.DetectContentType(content)
		if len(content) > 4 && string(content[:4]) == "%PDF" {
			mimeType = "application/pdf"
		}
		file = fmt.Sprintf("data:%s;base64,%s", mimeType, base64.StdEncoding.EncodeToString(content))
	}

	reqBody := map[string]interface{}{
		"model": *modelName,
		"file":  file,
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	url := fmt.Sprintf("%s/%s", baseURL, strings.TrimPrefix(z.baseModel.URLSuffix.OCR, "/"))
	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), longOpCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := z.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("ZhipuAI OCR API error: %s, body: %s", resp.Status, "[redacted]")
	}

	var zhipuResp zhipuOCRResponse
	if err = json.Unmarshal(body, &zhipuResp); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	if zhipuResp.MarkdownResults == nil {
		return nil, fmt.Errorf("ZhipuAI OCR API response missing md_results")
	}

	return &OCRFileResponse{Text: zhipuResp.MarkdownResults}, nil
}

// ParseFile parse file
func (z *ZhipuAIModel) ParseFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*ParseFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", z.Name())
}

func (z *ZhipuAIModel) ListTasks(apiConfig *APIConfig) ([]ListTaskStatus, error) {
	return nil, fmt.Errorf("%s, no such method", z.Name())
}

func (z *ZhipuAIModel) ShowTask(taskID string, apiConfig *APIConfig) (*TaskResponse, error) {
	return nil, fmt.Errorf("%s, no such method", z.Name())
}
