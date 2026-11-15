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
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
)

// HunyuanModel implements ModelDriver for Tencent Hunyuan
type HunyuanModel struct {
	baseModel BaseModel
}

// NewHunyuanModel creates a new Hunyuan model instance.
func NewHunyuanModel(baseURL map[string]string, urlSuffix URLSuffix) *HunyuanModel {
	if baseURL != nil {
		copied := make(map[string]string, len(baseURL))
		for region, value := range baseURL {
			copied[region] = value
		}
		baseURL = copied
	}
	return &HunyuanModel{
		baseModel: BaseModel{
			BaseURL:    baseURL,
			URLSuffix:  urlSuffix,
			httpClient: NewDriverHTTPClient(),
		},
	}
}

func (h *HunyuanModel) NewInstance(baseURL map[string]string) ModelDriver {
	return NewHunyuanModel(baseURL, h.baseModel.URLSuffix)
}

func (h *HunyuanModel) Name() string {
	return "hunyuan"
}

func (h *HunyuanModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	if strings.TrimSpace(modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if err := h.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}
	if len(messages) == 0 {
		return nil, fmt.Errorf("messages is empty")
	}

	baseURL, err := h.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL = strings.TrimSuffix(baseURL, "/")
	url := fmt.Sprintf("%s/%s", baseURL, h.baseModel.URLSuffix.Chat)

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
		"stream":   false,
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

	resp, err := h.baseModel.httpClient.Do(req)
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

	reasonContent := ""
	if r, ok := messageMap["reasoning_content"].(string); ok {
		reasonContent = r
	}

	return &ChatResponse{
		Answer:        &content,
		ReasonContent: &reasonContent,
	}, nil
}

// ChatStreamlyWithSender opens the SSE chat-completions
func (h *HunyuanModel) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig, sender func(*string, *string) error) error {
	if strings.TrimSpace(modelName) == "" {
		return fmt.Errorf("model name is required")
	}

	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	if err := h.baseModel.APIConfigCheck(apiConfig); err != nil {
		return err
	}

	if sender == nil {
		return fmt.Errorf("sender is required")
	}
	if len(messages) == 0 {
		return fmt.Errorf("messages is empty")
	}

	baseURL, err := h.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return err
	}
	baseURL = strings.TrimSuffix(baseURL, "/")
	url := fmt.Sprintf("%s/%s", baseURL, h.baseModel.URLSuffix.Chat)

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
		"stream":   true,
	}

	if chatModelConfig != nil {
		if chatModelConfig.Stream != nil && !*chatModelConfig.Stream {
			return fmt.Errorf("stream must be true in ChatStreamlyWithSender")
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
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return fmt.Errorf("failed to marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(apiConfig.RequestContext(), "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := h.baseModel.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
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

		event := rawEvent

		if apiErr, ok := event["error"]; ok {
			return fmt.Errorf("hunyuan: upstream stream error: %v", apiErr)
		}

		choices, ok := event["choices"].([]interface{})
		if !ok || len(choices) == 0 {
			return nil
		}
		firstChoice, ok := choices[0].(map[string]interface{})
		if !ok {
			return nil
		}
		if delta, ok := firstChoice["delta"].(map[string]interface{}); ok {
			if r, ok := delta["reasoning_content"].(string); ok && r != "" {
				rr := r
				if err := sender(nil, &rr); err != nil {
					return err
				}
				if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
					return parentErr
				}

			}
			if c, ok := delta["content"].(string); ok && c != "" {
				cc := c
				if err := sender(&cc, nil); err != nil {
					return err
				}
				if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
					return parentErr
				}

			}
		}
		if finish, ok := firstChoice["finish_reason"].(string); ok && finish != "" {
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
		return fmt.Errorf("hunyuan: stream ended before [DONE] or finish_reason")
	}
	if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
		return parentErr
	}
	if !responseHasContent || (!done && !responseHasTerminal) {
		return fmt.Errorf("stream ended before successful content completion")
	}

	endOfStream := "[DONE]"
	if err := sender(&endOfStream, nil); err != nil {
		return err
	}
	if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
		return parentErr
	}

	return nil
}

func (h *HunyuanModel) ListModels(apiConfig *APIConfig) ([]ListModelResponse, error) {
	if err := h.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	baseURL, err := h.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL = strings.TrimSuffix(baseURL, "/")
	url := fmt.Sprintf("%s/%s", baseURL, h.baseModel.URLSuffix.Models)

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := h.baseModel.httpClient.Do(req)
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
	var modelList ModelList
	var inventoryEnvelope map[string]json.RawMessage
	if json.Unmarshal(body, &inventoryEnvelope) != nil || inventoryEnvelope == nil || inventoryEnvelope["data"] == nil || string(inventoryEnvelope["data"]) == "null" {
		return nil, fmt.Errorf("models response missing data")
	}
	if value := inventoryEnvelope["error"]; len(value) > 0 && string(value) != "null" {
		return nil, fmt.Errorf("model discovery business error")
	}
	if code := inventoryEnvelope["code"]; len(code) > 0 && string(code) != "0" && string(code) != "\"0\"" && string(code) != "null" {
		return nil, fmt.Errorf("model discovery business error")
	}
	if err = json.Unmarshal(body, &modelList); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}
	if modelList.Models == nil {
		return nil, fmt.Errorf("invalid models list format")
	}

	return ParseListModel(modelList), nil
}

func (h *HunyuanModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := h.ListModels(apiConfig)
	return err
}

func (h *HunyuanModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	if len(texts) > 0 && (modelName == nil || strings.TrimSpace(*modelName) == "") {
		return nil, fmt.Errorf("model name is required")
	}

	if embeddingConfig != nil && embeddingConfig.Dimension < 0 {
		return nil, fmt.Errorf("embedding dimension must be nonnegative")
	}

	if err := h.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(texts) == 0 {
		return []EmbeddingData{}, nil
	}
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}

	baseURL, err := h.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL = strings.TrimSuffix(baseURL, "/")
	url := fmt.Sprintf("%s/%s", baseURL, h.baseModel.URLSuffix.Embedding)

	reqBody := map[string]interface{}{
		"model": *modelName,
		"input": texts,
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

	resp, err := h.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("Hunyuan embedding API error: status %d, body: %s", resp.StatusCode, "[redacted]")
	}

	var parsedResponse struct {
		Data []struct {
			Embedding []float64 `json:"embedding"`
			Index     int       `json:"index"`
		} `json:"data"`
	}

	var responseEnvelope map[string]json.RawMessage
	if json.Unmarshal(body, &responseEnvelope) != nil || responseEnvelope == nil || responseEnvelope["error"] != nil {
		return nil, fmt.Errorf("invalid embedding response")
	}
	if code := responseEnvelope["code"]; code != nil {
		var n float64
		if json.Unmarshal(code, &n) == nil && n != 0 {
			return nil, fmt.Errorf("upstream embedding error")
		}
	}
	var numericRows []EmbeddingData
	if json.Unmarshal(responseEnvelope["data"], &numericRows) != nil || len(numericRows) != len(texts) {
		return nil, fmt.Errorf("invalid embedding count or values")
	}
	seenNumeric := map[int]bool{}
	numericDimension := 0
	for _, row := range numericRows {
		if row.Index < 0 || row.Index >= len(texts) || seenNumeric[row.Index] {
			return nil, fmt.Errorf("invalid embedding identity")
		}
		seenNumeric[row.Index] = true
		if numericDimension == 0 {
			numericDimension = len(row.Embedding)
		} else if numericDimension != len(row.Embedding) {
			return nil, fmt.Errorf("inconsistent embedding dimension")
		}
		if embeddingConfig != nil && embeddingConfig.Dimension > 0 && len(row.Embedding) != embeddingConfig.Dimension {
			return nil, fmt.Errorf("unexpected embedding dimension")
		}
	}
	if err = json.Unmarshal(body, &parsedResponse); err != nil {
		return nil, fmt.Errorf("failed to decode response: %w", err)
	}

	if len(parsedResponse.Data) == 0 {
		return nil, fmt.Errorf("hunyuan embedding response contains no data: %s", "[redacted]")
	}

	embeddings := make([]EmbeddingData, 0, len(parsedResponse.Data))
	for _, dataElem := range parsedResponse.Data {
		embeddings = append(embeddings, EmbeddingData{
			Embedding: dataElem.Embedding,
			Index:     dataElem.Index,
		})
	}

	return embeddings, nil
}

func (h *HunyuanModel) Rerank(modelName *string, query string, documents []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return nil, fmt.Errorf("%s, no such method", h.Name())
}

func (h *HunyuanModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("%s, no such method", h.Name())
}

func (h *HunyuanModel) TranscribeAudio(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig) (*ASRResponse, error) {
	return nil, fmt.Errorf("%s, no such method", h.Name())
}

func (h *HunyuanModel) TranscribeAudioWithSender(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", h.Name())
}

func (h *HunyuanModel) AudioSpeech(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig) (*TTSResponse, error) {
	return nil, fmt.Errorf("%s, no such method", h.Name())
}

func (h *HunyuanModel) AudioSpeechWithSender(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", h.Name())
}

func (h *HunyuanModel) OCRFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, ocrConfig *OCRConfig) (*OCRFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", h.Name())
}

func (h *HunyuanModel) ParseFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*ParseFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", h.Name())
}

func (h *HunyuanModel) ListTasks(apiConfig *APIConfig) ([]ListTaskStatus, error) {
	return nil, fmt.Errorf("%s, no such method", h.Name())
}

func (h *HunyuanModel) ShowTask(taskID string, apiConfig *APIConfig) (*TaskResponse, error) {
	return nil, fmt.Errorf("%s, no such method", h.Name())
}
