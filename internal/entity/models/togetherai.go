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
	"strconv"
	"strings"
)

type TogetherAIModel struct {
	baseModel BaseModel
}

func NewTogetherAIModel(baseURL map[string]string, urlSuffix URLSuffix) *TogetherAIModel {
	if baseURL != nil {
		copied := make(map[string]string, len(baseURL))
		for region, value := range baseURL {
			copied[region] = value
		}
		baseURL = copied
	}
	return &TogetherAIModel{
		baseModel: BaseModel{
			BaseURL:    baseURL,
			URLSuffix:  urlSuffix,
			httpClient: NewDriverHTTPClient(),
		},
	}
}

func (t *TogetherAIModel) NewInstance(baseURL map[string]string) ModelDriver {
	return NewTogetherAIModel(baseURL, t.baseModel.URLSuffix)
}

func (t *TogetherAIModel) Name() string {
	return "togetherai"
}

type togetherAIReasoningOptions struct {
	Enabled bool `json:"enabled"`
}

func (t *TogetherAIModel) chatPayload(modelName string, messages []Message, stream bool, chatModelConfig *ChatConfig) map[string]interface{} {
	apiMessages := make([]map[string]interface{}, len(messages))
	for i, msg := range messages {
		apiMessages[i] = map[string]interface{}{
			"role":    msg.Role,
			"content": msg.Content,
		}
	}

	reqBody := map[string]interface{}{
		"model":    modelName,
		"messages": apiMessages,
		"stream":   stream,
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
		if chatModelConfig.Stop != nil {
			reqBody["stop"] = *chatModelConfig.Stop
		}
		if chatModelConfig.Thinking != nil {
			reqBody["reasoning"] = togetherAIReasoningOptions{
				Enabled: *chatModelConfig.Thinking,
			}
		}
		if chatModelConfig.Effort != nil && strings.Contains(strings.ToLower(modelName), "gpt-oss") {
			reqBody["reasoning_effort"] = *chatModelConfig.Effort
		}
	}

	return reqBody
}

func (t *TogetherAIModel) chatURL(apiConfig *APIConfig) (string, error) {

	baseURL, err := t.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return "", err
	}
	baseURL = strings.TrimSuffix(baseURL, "/")
	return fmt.Sprintf("%s/%s", baseURL, t.baseModel.URLSuffix.Chat), nil
}

type togetherAIChatMessage struct {
	Content          string `json:"content"`
	ReasoningContent string `json:"reasoning_content"`
	Reasoning        string `json:"reasoning"`
}

type togetherAIChatChoice struct {
	Message      togetherAIChatMessage `json:"message"`
	Delta        togetherAIChatMessage `json:"delta"`
	FinishReason string                `json:"finish_reason"`
}

type togetherAIChatResponse struct {
	Choices      []togetherAIChatChoice `json:"choices"`
	Error        interface{}            `json:"error"`
	FinishReason string                 `json:"finish_reason"`
}

func (t *TogetherAIModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	if err := t.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}
	if strings.TrimSpace(modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}
	if len(messages) == 0 {
		return nil, fmt.Errorf("messages is empty")
	}

	url, err := t.chatURL(apiConfig)
	if err != nil {
		return nil, err
	}

	jsonData, err := json.Marshal(t.chatPayload(modelName, messages, false, chatModelConfig))
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := t.baseModel.httpClient.Do(req)
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

	var result togetherAIChatResponse

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
	if result.Error != nil {
		return nil, fmt.Errorf("togetherai: upstream error: %v", result.Error)
	}
	if len(result.Choices) == 0 {
		return nil, fmt.Errorf("no choices in response")
	}

	content := result.Choices[0].Message.Content
	reasonContent := result.Choices[0].Message.ReasoningContent
	if reasonContent == "" {
		reasonContent = result.Choices[0].Message.Reasoning
	}
	return &ChatResponse{
		Answer:        &content,
		ReasonContent: &reasonContent,
	}, nil
}

func (t *TogetherAIModel) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	if err := t.baseModel.APIConfigCheck(apiConfig); err != nil {
		return err
	}

	if sender == nil {
		return fmt.Errorf("sender is required")
	}
	if strings.TrimSpace(modelName) == "" {
		return fmt.Errorf("model name is required")
	}
	if len(messages) == 0 {
		return fmt.Errorf("messages is empty")
	}
	if chatModelConfig != nil && chatModelConfig.Stream != nil && !*chatModelConfig.Stream {
		return fmt.Errorf("stream must be true in ChatStreamlyWithSender")
	}

	url, err := t.chatURL(apiConfig)
	if err != nil {
		return err
	}

	jsonData, err := json.Marshal(t.chatPayload(modelName, messages, true, chatModelConfig))
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
	req.Header.Set("Accept", "text/event-stream")

	resp, err := t.baseModel.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 20*1024*1024))
		return fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	sawTerminal := false
	responseHasContent, responseHasTerminal := false, false
	done, err := ParseSSEStream[map[string]interface{}](resp.Body, func(rawEvent map[string]interface{}) error {
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

		rawJSON, _ := json.Marshal(rawEvent)
		var event togetherAIChatResponse
		if err := json.Unmarshal(rawJSON, &event); err != nil {
			return fmt.Errorf("invalid stream event: %w", err)
		}

		if event.Error != nil {
			return fmt.Errorf("togetherai: upstream stream error: %v", event.Error)
		}
		if len(event.Choices) == 0 {
			return nil
		}

		choice := event.Choices[0]
		if choice.Delta.ReasoningContent != "" {
			if err := sender(nil, &choice.Delta.ReasoningContent); err != nil {
				return err
			}
			if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
				return parentErr
			}

		}
		if choice.Delta.Reasoning != "" {
			if err := sender(nil, &choice.Delta.Reasoning); err != nil {
				return err
			}
			if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
				return parentErr
			}

		}
		if choice.Delta.Content != "" {
			if err := sender(&choice.Delta.Content, nil); err != nil {
				return err
			}
			if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
				return parentErr
			}

		}
		if choice.FinishReason != "" || event.FinishReason != "" {
			sawTerminal = true
		}
		return nil
	})
	if err != nil {
		if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
			return parentErr
		}
		return fmt.Errorf("failed to scan response body: %w", err)
	}
	if !done && !sawTerminal && !responseHasTerminal {
		return fmt.Errorf("togetherai: stream ended before [DONE] or finish_reason")
	}
	if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
		return parentErr
	}
	if !responseHasContent || (!done && !responseHasTerminal) {
		return fmt.Errorf("stream ended before successful content completion")
	}

	endOfStream := "[DONE]"
	return sender(&endOfStream, nil)
}

type togetherAIModelInfo struct {
	ID string `json:"id"`
}

func (t *TogetherAIModel) ListModels(apiConfig *APIConfig) ([]ListModelResponse, error) {
	if err := t.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	baseURL, err := t.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL = strings.TrimSuffix(baseURL, "/")
	url := fmt.Sprintf("%s/%s", baseURL, t.baseModel.URLSuffix.Models)

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := t.baseModel.httpClient.Do(req)
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

	var identities []struct {
		ID *string `json:"id"`
	}
	if json.Unmarshal(body, &identities) != nil || identities == nil {
		return nil, fmt.Errorf("invalid TogetherAI inventory")
	}
	for _, item := range identities {
		if item.ID == nil || strings.TrimSpace(*item.ID) == "" {
			return nil, fmt.Errorf("invalid model identity")
		}
	}
	var result []DSModel
	if err = json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	return ParseListModel(ModelList{Models: result}), nil
}

func (t *TogetherAIModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := t.ListModels(apiConfig)
	return err
}

type togetherAIEmbeddingData struct {
	Embedding []float64 `json:"embedding"`
	Object    string    `json:"object"`
	Index     int       `json:"index"`
}

type togetherAIEmbeddingResponse struct {
	Data   []togetherAIEmbeddingData `json:"data"`
	Model  string                    `json:"model"`
	Object string                    `json:"object"`
}

func (t *TogetherAIModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	if len(texts) > 0 && (modelName == nil || strings.TrimSpace(*modelName) == "") {
		return nil, fmt.Errorf("model name is required")
	}

	if embeddingConfig != nil && embeddingConfig.Dimension < 0 {
		return nil, fmt.Errorf("embedding dimension must be nonnegative")
	}

	if err := t.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(texts) == 0 {
		return []EmbeddingData{}, nil
	}

	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}

	baseURL, err := t.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL = strings.TrimSuffix(baseURL, "/")
	url := fmt.Sprintf("%s/%s", baseURL, t.baseModel.URLSuffix.Embedding)

	reqBody := map[string]interface{}{
		"model": *modelName,
		"input": texts,
	}
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

	resp, err := t.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("TogetherAI embeddings API error: %s, body: %s", resp.Status, "[redacted]")
	}

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

	var parsed togetherAIEmbeddingResponse
	if err = json.Unmarshal(body, &parsed); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	embeddings := make([]EmbeddingData, len(texts))
	filled := make([]bool, len(texts))
	for _, item := range parsed.Data {
		if item.Index < 0 || item.Index >= len(texts) {
			return nil, fmt.Errorf("togetherai: response index %d out of range for %d inputs", item.Index, len(texts))
		}
		if filled[item.Index] {
			return nil, fmt.Errorf("togetherai: duplicate embedding index %d in response", item.Index)
		}
		embeddings[item.Index] = EmbeddingData{
			Embedding: item.Embedding,
			Index:     item.Index,
		}
		filled[item.Index] = true
	}
	for i, ok := range filled {
		if !ok {
			return nil, fmt.Errorf("togetherai: missing embedding for input index %d", i)
		}
	}

	return embeddings, nil
}

func (t *TogetherAIModel) Rerank(modelName *string, query string, documents []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
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

	if rerankConfig == nil {
		rerankConfig = &RerankConfig{}
	}
	if rerankConfig.TopN < 0 {
		return nil, fmt.Errorf("top_n must be nonnegative")
	}
	if len(documents) > 0 && (modelName == nil || strings.TrimSpace(*modelName) == "") {
		return nil, fmt.Errorf("model name is required")
	}

	if err := t.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(documents) == 0 {
		return &RerankResponse{}, nil
	}

	resolvedBaseURL, err := t.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, t.baseModel.URLSuffix.Rerank)

	var topN = rerankConfig.TopN
	if rerankConfig.TopN != 0 {
		topN = rerankConfig.TopN
	}

	reqBody := map[string]interface{}{
		"model":     *modelName,
		"query":     query,
		"documents": documents,
		"top_n":     topN,
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(apiConfig.RequestContext(), "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := t.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("TogetherAI Rerank API error: status %d, body: %s", resp.StatusCode, "[redacted]")
	}

	var raw struct {
		Results []RerankResult `json:"results"`
	}
	if json.Unmarshal(body, &raw) != nil {
		return nil, fmt.Errorf("invalid rerank numeric values")
	}
	expected := len(documents)
	if rerankConfig.TopN > 0 && rerankConfig.TopN < expected {
		expected = rerankConfig.TopN
	}
	if len(raw.Results) != expected {
		return nil, fmt.Errorf("invalid rerank count")
	}
	seen := map[int]bool{}
	for _, row := range raw.Results {
		if row.Index < 0 || row.Index >= len(documents) || seen[row.Index] {
			return nil, fmt.Errorf("invalid rerank identity")
		}
		seen[row.Index] = true
	}

	var rerankResp struct {
		Results []struct {
			Index          int     `json:"index"`
			RelevanceScore float64 `json:"relevance_score"`
		} `json:"results"`
	}

	if err = json.Unmarshal(body, &rerankResp); err != nil {
		return nil, fmt.Errorf("failed to decode response: %w", err)
	}

	var rerankResponse RerankResponse
	for _, result := range rerankResp.Results {
		rerankResult := RerankResult{
			Index:          result.Index,
			RelevanceScore: result.RelevanceScore,
		}
		rerankResponse.Data = append(rerankResponse.Data, rerankResult)
	}

	return &rerankResponse, nil
}

func (t *TogetherAIModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("%s, no such method", t.Name())
}

func (t *TogetherAIModel) TranscribeAudio(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig) (*ASRResponse, error) {
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

	if err := t.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if file == nil || *file == "" {
		return nil, fmt.Errorf("file is missing")
	}

	resolvedBaseURL, err := t.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, t.baseModel.URLSuffix.ASR)

	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	if err := writer.WriteField("model", *modelName); err != nil {
		return nil, err
	}

	// audio file
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

	// extra params
	if asrConfig != nil && asrConfig.Params != nil {
		for key, value := range asrConfig.Params {

			var val string

			switch v := value.(type) {
			case string:
				val = v
			case bool:
				val = strconv.FormatBool(v)
			case int:
				val = strconv.Itoa(v)
			case float64:
				val = strconv.FormatFloat(v, 'f', -1, 64)
			default:
				val = fmt.Sprintf("%v", v)
			}

			if err := writer.WriteField(key, val); err != nil {
				return nil, fmt.Errorf("failed to write field %s: %w", key, err)
			}
		}
	}

	if err := writer.Close(); err != nil {
		return nil, fmt.Errorf("failed to close multipart writer: %w", err)
	}

	// request
	req, err := http.NewRequestWithContext(apiConfig.RequestContext(), "POST", url, &body)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))
	req.Header.Set("Content-Type", writer.FormDataContentType())

	resp, err := t.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("TogetherAI ASR error: %s - %s", resp.Status, "[redacted]")
	}

	// result
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

	if err := json.Unmarshal(respBody, &result); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response: %w", err)
	}

	return &ASRResponse{
		Text: result.Text,
	}, nil
}

func (t *TogetherAIModel) TranscribeAudioWithSender(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", t.Name())
}

func (t *TogetherAIModel) AudioSpeech(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig) (*TTSResponse, error) {
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if err := t.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if audioContent == nil || *audioContent == "" {
		return nil, fmt.Errorf("text content is missing")
	}

	resolvedBaseURL, err := t.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, t.baseModel.URLSuffix.TTS)

	reqBody := map[string]interface{}{
		"model": *modelName,
		"input": *audioContent,
	}

	if ttsConfig != nil && ttsConfig.Params != nil {
		for key, value := range ttsConfig.Params {
			reqBody[key] = value
		}
	}
	if ttsConfig != nil && ttsConfig.Format != "" {
		reqBody["response_format"] = ttsConfig.Format
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(apiConfig.RequestContext(), "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := t.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%s - %s", resp.Status, "[redacted]")
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

func (t *TogetherAIModel) AudioSpeechWithSender(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, sender func(*string, *string) error) error {
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return fmt.Errorf("model name is required")
	}
	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	if err := t.baseModel.APIConfigCheck(apiConfig); err != nil {
		return err
	}

	if audioContent == nil || *audioContent == "" {
		return fmt.Errorf("text content is missing")
	}

	resolvedBaseURL, err := t.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return err
	}
	cleanBaseURL := strings.TrimRight(resolvedBaseURL, "/")
	cleanSuffix := strings.TrimLeft(t.baseModel.URLSuffix.TTS, "/")
	url := fmt.Sprintf("%s/%s", cleanBaseURL, cleanSuffix)

	// Build Request body
	reqBody := map[string]interface{}{
		"model":  *modelName,
		"input":  *audioContent,
		"stream": true,
	}

	if ttsConfig != nil {
		if ttsConfig.Format != "" {
			reqBody["response_format"] = ttsConfig.Format
		}
		if ttsConfig.Params != nil {
			for key, value := range ttsConfig.Params {
				reqBody[key] = value
			}
		}
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return fmt.Errorf("failed to marshal request: %w", err)
	}

	// Build Request
	req, err := http.NewRequestWithContext(apiConfig.RequestContext(), "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := t.baseModel.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("TogetherAI stream API error: %d - %s", resp.StatusCode, "[redacted]")
	}

	sawAudio := false
	done, err := ParseSSEStream[map[string]interface{}](resp.Body, func(event map[string]interface{}) error {
		if err := apiConfig.RequestContext().Err(); err != nil {
			return err
		}
		if event == nil || event["error"] != nil {
			return fmt.Errorf("upstream speech stream error")
		}
		if event["type"] != "conversation.item.audio_output.delta" {
			return nil
		}
		delta, ok := event["delta"].(string)
		if !ok {
			return fmt.Errorf("invalid speech delta")
		}
		audio, err := base64.StdEncoding.Strict().DecodeString(delta)
		if err != nil || len(audio) == 0 {
			return fmt.Errorf("invalid speech audio data")
		}
		sawAudio = true
		chunk := string(audio)
		if err := sender(&chunk, nil); err != nil {
			return err
		}
		return apiConfig.RequestContext().Err()
	})
	if err := apiConfig.RequestContext().Err(); err != nil {
		return err
	}
	if err != nil {
		return err
	}
	if !done || !sawAudio {
		return fmt.Errorf("incomplete speech stream")
	}
	return nil
}

func (t *TogetherAIModel) OCRFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, ocrConfig *OCRConfig) (*OCRFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", t.Name())
}

func (t *TogetherAIModel) ParseFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*ParseFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", t.Name())
}

func (t *TogetherAIModel) ListTasks(apiConfig *APIConfig) ([]ListTaskStatus, error) {
	return nil, fmt.Errorf("%s, no such method", t.Name())
}

func (t *TogetherAIModel) ShowTask(taskID string, apiConfig *APIConfig) (*TaskResponse, error) {
	return nil, fmt.Errorf("%s, no such method", t.Name())
}
