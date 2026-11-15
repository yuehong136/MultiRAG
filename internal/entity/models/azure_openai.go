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
	"net/url"
	"strings"
)

// azureAPIVersion is the Azure OpenAI REST API version sent as the
const azureAPIVersion = "2024-10-21"

// AzureOpenAIModel implements ModelDriver for Azure OpenAI.
type AzureOpenAIModel struct {
	baseModel BaseModel
}

// NewAzureOpenAIModel creates a new Azure OpenAI model instance.
func NewAzureOpenAIModel(baseURL map[string]string, urlSuffix URLSuffix) *AzureOpenAIModel {
	if baseURL != nil {
		copied := make(map[string]string, len(baseURL))
		for region, value := range baseURL {
			copied[region] = value
		}
		baseURL = copied
	}
	return &AzureOpenAIModel{
		baseModel: BaseModel{
			BaseURL:    baseURL,
			URLSuffix:  urlSuffix,
			httpClient: NewDriverHTTPClient(),
		},
	}
}

func (a *AzureOpenAIModel) NewInstance(baseURL map[string]string) ModelDriver {
	return NewAzureOpenAIModel(baseURL, a.baseModel.URLSuffix)
}

func (a *AzureOpenAIModel) Name() string {
	return "azure-openai"
}

// deploymentURL builds a deployment-scoped data-plane URL of the form
func (a *AzureOpenAIModel) deploymentURL(baseURL, deployment, op string) string {
	return fmt.Sprintf("%s/deployments/%s/%s?api-version=%s",
		strings.TrimRight(baseURL, "/"), url.PathEscape(deployment), op, azureAPIVersion)
}

// ChatWithMessages sends multiple messages with roles and returns the response.
func (a *AzureOpenAIModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	if err := a.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(messages) == 0 {
		return nil, fmt.Errorf("messages is empty")
	}

	if modelName == "" {
		return nil, fmt.Errorf("deployment name is required")
	}

	baseURL, err := a.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL = strings.TrimSuffix(baseURL, "/")
	url := a.deploymentURL(baseURL, modelName, a.baseModel.URLSuffix.Chat)

	apiMessages := make([]map[string]interface{}, len(messages))
	for i, msg := range messages {
		apiMessages[i] = map[string]interface{}{
			"role":    msg.Role,
			"content": msg.Content,
		}
	}

	reqBody := map[string]interface{}{
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
	req.Header.Set("api-key", *apiConfig.ApiKey)

	resp, err := a.baseModel.httpClient.Do(req)
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
	if raw := responseChoices[0]["content_filter_results"]; raw != nil {
		var filters map[string]struct {
			Filtered *bool `json:"filtered"`
		}
		if json.Unmarshal(raw, &filters) != nil || filters == nil {
			return nil, fmt.Errorf("invalid Azure content filter")
		}
		for _, filter := range filters {
			if filter.Filtered == nil || *filter.Filtered {
				return nil, fmt.Errorf("Azure response was filtered")
			}
		}
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
	if rc, ok := messageMap["reasoning_content"].(string); ok {
		reasonContent = rc
		if reasonContent != "" && reasonContent[0] == '\n' {
			reasonContent = reasonContent[1:]
		}
	}

	return &ChatResponse{
		Answer:        &content,
		ReasonContent: &reasonContent,
	}, nil
}

// ChatStreamlyWithSender sends messages and streams the response via the
// sender function. Used for streaming chat responses with no extra channel.
func (a *AzureOpenAIModel) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	ctx := apiConfig.RequestContext()

	if err := a.baseModel.APIConfigCheck(apiConfig); err != nil {
		return err
	}

	if len(messages) == 0 {
		return fmt.Errorf("messages is empty")
	}

	if modelName == "" {
		return fmt.Errorf("deployment name is required")
	}

	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	baseURL, err := a.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return err
	}
	baseURL = strings.TrimSuffix(baseURL, "/")
	url := a.deploymentURL(baseURL, modelName, a.baseModel.URLSuffix.Chat)

	apiMessages := make([]map[string]interface{}, len(messages))
	for i, msg := range messages {
		apiMessages[i] = map[string]interface{}{
			"role":    msg.Role,
			"content": msg.Content,
		}
	}

	reqBody := map[string]interface{}{
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
	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("api-key", *apiConfig.ApiKey)

	resp, err := a.baseModel.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 20*1024*1024))
		return fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	sawTerminal, sawContent := false, false
	done, err := ParseSSEStream[map[string]interface{}](resp.Body, func(event map[string]interface{}) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		if event == nil || event["error"] != nil {
			return fmt.Errorf("upstream stream error")
		}
		hasMetadata := false
		for _, field := range []string{"prompt_filter_results", "prompt_annotations"} {
			if raw, exists := event[field]; exists {
				annotations, ok := raw.([]interface{})
				if !ok {
					return fmt.Errorf("invalid Azure prompt annotations")
				}
				for _, rawAnnotation := range annotations {
					annotation, ok := rawAnnotation.(map[string]interface{})
					if !ok {
						return fmt.Errorf("invalid Azure prompt annotation")
					}
					index, ok := annotation["prompt_index"].(float64)
					if !ok || index < 0 || index != float64(int(index)) {
						return fmt.Errorf("invalid Azure prompt index")
					}
					filters, ok := annotation["content_filter_results"].(map[string]interface{})
					if !ok {
						return fmt.Errorf("invalid Azure prompt filter")
					}
					for _, rawFilter := range filters {
						filter, ok := rawFilter.(map[string]interface{})
						if !ok {
							return fmt.Errorf("invalid Azure prompt filter")
						}
						filtered, ok := filter["filtered"].(bool)
						if !ok || filtered {
							return fmt.Errorf("Azure prompt was filtered")
						}
					}
				}
				hasMetadata = true
			}
		}
		if _, ok := event["usage"].(map[string]interface{}); ok {
			hasMetadata = true
		}
		choices, ok := event["choices"].([]interface{})
		if !ok {
			return fmt.Errorf("invalid stream choices")
		}
		if len(choices) == 0 {
			if hasMetadata {
				return nil
			}
			return fmt.Errorf("invalid stream choices")
		}
		choice, ok := choices[0].(map[string]interface{})
		if !ok {
			return fmt.Errorf("invalid stream choice")
		}
		if raw, exists := choice["content_filter_results"]; exists {
			filters, ok := raw.(map[string]interface{})
			if !ok {
				return fmt.Errorf("invalid Azure content filter")
			}
			for _, rawFilter := range filters {
				filter, ok := rawFilter.(map[string]interface{})
				if !ok {
					return fmt.Errorf("invalid Azure content filter")
				}
				filtered, ok := filter["filtered"].(bool)
				if !ok || filtered {
					return fmt.Errorf("Azure response was filtered")
				}
			}
			if choice["delta"] == nil && choice["finish_reason"] == nil {
				offsets, ok := choice["content_filter_offsets"].(map[string]interface{})
				if !ok {
					return fmt.Errorf("invalid Azure filter offsets")
				}
				start, startOK := offsets["start_offset"].(float64)
				end, endOK := offsets["end_offset"].(float64)
				check, checkOK := offsets["check_offset"].(float64)
				if !startOK || !endOK || !checkOK || start < 0 || end < start || check < 0 || start != float64(int(start)) || end != float64(int(end)) || check != float64(int(check)) {
					return fmt.Errorf("invalid Azure filter offsets")
				}
				return nil
			}
		}
		terminal := false
		if value := choice["finish_reason"]; value != nil {
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
		}
		delta, ok := choice["delta"].(map[string]interface{})
		if !ok {
			if !terminal || choice["delta"] != nil {
				return fmt.Errorf("invalid stream delta")
			}
			delta = map[string]interface{}{}
		}
		for _, field := range []string{"reasoning_content", "content"} {
			if value, exists := delta[field]; exists && value != nil {
				text, ok := value.(string)
				if !ok {
					return fmt.Errorf("invalid stream text")
				}
				if text != "" {
					if field == "content" {
						sawContent = true
					}
					var err error
					if field == "content" {
						err = sender(&text, nil)
					} else {
						err = sender(nil, &text)
					}
					if err != nil {
						return err
					}
					if err := ctx.Err(); err != nil {
						return err
					}
				}
			}
		}
		if terminal {
			sawTerminal = true
		}
		return nil
	})
	if err := ctx.Err(); err != nil {
		return err
	}
	if err != nil {
		return fmt.Errorf("failed to scan response body: %w", err)
	}
	if (!done && !sawTerminal) || !sawContent {
		return fmt.Errorf("incomplete stream response")
	}

	endOfStream := "[DONE]"
	return sender(&endOfStream, nil)
}

type azureEmbeddingResponse struct {
	Data []struct {
		Embedding []float64 `json:"embedding"`
		Index     int       `json:"index"`
	} `json:"data"`
}

// Embed turns a list of texts into embedding vectors
func (a *AzureOpenAIModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	if len(texts) > 0 && (modelName == nil || strings.TrimSpace(*modelName) == "") {
		return nil, fmt.Errorf("model name is required")
	}

	if embeddingConfig != nil && embeddingConfig.Dimension < 0 {
		return nil, fmt.Errorf("embedding dimension must be nonnegative")
	}

	if err := a.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(texts) == 0 {
		return []EmbeddingData{}, nil
	}

	if modelName == nil || *modelName == "" {
		return nil, fmt.Errorf("deployment name is required")
	}

	baseURL, err := a.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL = strings.TrimSuffix(baseURL, "/")
	url := a.deploymentURL(baseURL, *modelName, a.baseModel.URLSuffix.Embedding)

	// As with chat, the deployment is in the URL path, so no "model" field.
	reqBody := map[string]interface{}{
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
	req.Header.Set("api-key", *apiConfig.ApiKey)

	resp, err := a.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("Azure OpenAI embeddings API error: %s, body: %s", resp.Status, "[redacted]")
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

	var parsed azureEmbeddingResponse
	if err = json.Unmarshal(body, &parsed); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}
	embeddings := make([]EmbeddingData, len(texts))
	for _, d := range parsed.Data {
		if d.Index < 0 || d.Index >= len(embeddings) {
			return nil, fmt.Errorf("embedding index %d out of range [0,%d)", d.Index, len(embeddings))
		}
		embeddings[d.Index] = EmbeddingData{
			Embedding: d.Embedding,
			Index:     d.Index,
		}
	}

	return embeddings, nil
}

// ListModels returns the deployment names visible to the configured API key.
func (a *AzureOpenAIModel) ListModels(apiConfig *APIConfig) ([]ListModelResponse, error) {
	if err := a.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	baseURL, err := a.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL = strings.TrimSuffix(baseURL, "/")
	url := fmt.Sprintf("%s/%s?api-version=%s",
		strings.TrimRight(baseURL, "/"), a.baseModel.URLSuffix.Models, azureAPIVersion)

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("api-key", *apiConfig.ApiKey)

	resp, err := a.baseModel.httpClient.Do(req)
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
	var inventory map[string]json.RawMessage
	if err := json.Unmarshal(body, &inventory); err != nil || inventory == nil || inventory["data"] == nil || string(inventory["data"]) == "null" {
		return nil, fmt.Errorf("invalid model inventory")
	}
	if value := inventory["error"]; value != nil && string(value) != "null" {
		return nil, fmt.Errorf("upstream inventory error")
	}

	var modelList ModelList
	if err = json.Unmarshal(body, &modelList); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}
	if modelList.Models == nil {
		return nil, fmt.Errorf("invalid models list format")
	}

	return ParseListModel(modelList), nil
}

// CheckConnection runs a lightweight ListModels call to verify the endpoint
func (a *AzureOpenAIModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := a.ListModels(apiConfig)
	return err
}

// Balance is not exposed by the Azure OpenAI API.
func (a *AzureOpenAIModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("no such method")
}

// Rerank is not exposed by the Azure OpenAI API.
func (a *AzureOpenAIModel) Rerank(modelName *string, query string, documents []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return nil, fmt.Errorf("%s, no such method", a.Name())
}

func (a *AzureOpenAIModel) TranscribeAudio(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig) (*ASRResponse, error) {
	return nil, fmt.Errorf("%s, no such method", a.Name())
}

func (a *AzureOpenAIModel) TranscribeAudioWithSender(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", a.Name())
}

func (a *AzureOpenAIModel) AudioSpeech(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig) (*TTSResponse, error) {
	return nil, fmt.Errorf("%s, no such method", a.Name())
}

func (a *AzureOpenAIModel) AudioSpeechWithSender(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", a.Name())
}

func (a *AzureOpenAIModel) OCRFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, ocrConfig *OCRConfig) (*OCRFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", a.Name())
}

func (a *AzureOpenAIModel) ParseFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*ParseFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", a.Name())
}

func (a *AzureOpenAIModel) ListTasks(apiConfig *APIConfig) ([]ListTaskStatus, error) {
	return nil, fmt.Errorf("%s, no such method", a.Name())
}

func (a *AzureOpenAIModel) ShowTask(taskID string, apiConfig *APIConfig) (*TaskResponse, error) {
	return nil, fmt.Errorf("%s, no such method", a.Name())
}
