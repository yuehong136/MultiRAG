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

type OrcaRouterModel struct {
	baseModel BaseModel
}

func NewOrcaRouterModel(baseURL map[string]string, urlSuffix URLSuffix) *OrcaRouterModel {
	if baseURL != nil {
		copied := make(map[string]string, len(baseURL))
		for region, value := range baseURL {
			copied[region] = value
		}
		baseURL = copied
	}
	return &OrcaRouterModel{
		baseModel: BaseModel{
			BaseURL:    baseURL,
			URLSuffix:  urlSuffix,
			httpClient: NewDriverHTTPClient(),
		},
	}
}

func (o *OrcaRouterModel) NewInstance(baseURL map[string]string) ModelDriver {
	return NewOrcaRouterModel(baseURL, o.baseModel.URLSuffix)
}

func (o *OrcaRouterModel) Name() string {
	return "orcarouter"
}

func (o *OrcaRouterModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	if modelName == "" {
		return nil, fmt.Errorf("model name is required")
	}
	for _, message := range messages {
		if message.Content != nil {
			switch message.Content.(type) {
			case string, []any, []map[string]any:
			default:
				return nil, fmt.Errorf("invalid message content")
			}
		}
	}
	if chatModelConfig != nil && chatModelConfig.MaxTokens != nil && *chatModelConfig.MaxTokens < 0 {
		return nil, fmt.Errorf("max_tokens must be nonnegative")
	}

	if err := o.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}
	if len(messages) == 0 {
		return nil, fmt.Errorf("messages is empty")
	}

	resolvedBaseURL, err := o.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, o.baseModel.URLSuffix.Chat)

	// Convert messages to API format
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
		if chatModelConfig.Temperature != nil {
			reqBody["temperature"] = *chatModelConfig.Temperature
		}

		if chatModelConfig.MaxTokens != nil {
			reqBody["max_tokens"] = *chatModelConfig.MaxTokens
		}

		if chatModelConfig.Stream != nil {
			reqBody["stream"] = *chatModelConfig.Stream
		}

		if chatModelConfig.TopP != nil {
			reqBody["top_p"] = *chatModelConfig.TopP
		}
		// TODO gpt and claude
		if chatModelConfig.Effort != nil {
			reqBody["reasoning_effort"] = *chatModelConfig.Effort
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

	req.Header.Add("Content-Type", "application/json")
	req.Header.Add("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := o.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("failed to send request: %d %s", resp.StatusCode, "[redacted]")
	}

	// Parse response
	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response: %w", err)
	}

	if result == nil || result["error"] != nil {
		return nil, fmt.Errorf("upstream chat error")
	}
	if code, ok := result["code"].(float64); ok && code != 0 {
		return nil, fmt.Errorf("upstream chat error")
	}

	choices, ok := result["choices"].([]interface{})
	if !ok || len(choices) == 0 {
		return nil, fmt.Errorf("no choices in response")
	}

	firstChoice, ok := choices[0].(map[string]interface{})
	if !ok {
		return nil, fmt.Errorf("no choices in response")
	}

	messageMap, ok := firstChoice["message"].(map[string]interface{})
	if !ok {
		return nil, fmt.Errorf("no message in response")
	}

	if value, exists := messageMap["reasoning_content"]; exists && value != nil {
		if _, ok := value.(string); !ok {
			return nil, fmt.Errorf("invalid reasoning content format")
		}
	}

	content, ok := messageMap["content"].(string)
	if !ok {
		return nil, fmt.Errorf("no message in response")
	}

	// baichuan not support think
	emptyReason := ""
	chatResponse := &ChatResponse{
		Answer:        &content,
		ReasonContent: &emptyReason,
	}

	return chatResponse, nil
}

func (o *OrcaRouterModel) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, modelConfig *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	if modelName == "" {
		return fmt.Errorf("model name is required")
	}
	if sender == nil {
		return fmt.Errorf("sender is required")
	}
	for _, message := range messages {
		if message.Content != nil {
			switch message.Content.(type) {
			case string, []any, []map[string]any:
			default:
				return fmt.Errorf("invalid message content")
			}
		}
	}
	if modelConfig != nil && modelConfig.MaxTokens != nil && *modelConfig.MaxTokens < 0 {
		return fmt.Errorf("max_tokens must be nonnegative")
	}
	if modelConfig != nil && modelConfig.Stream != nil && !*modelConfig.Stream {
		return fmt.Errorf("stream must be true")
	}

	if err := o.baseModel.APIConfigCheck(apiConfig); err != nil {
		return err
	}

	if len(messages) == 0 {
		return fmt.Errorf("messages is empty")
	}

	resolvedBaseURL, err := o.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, o.baseModel.URLSuffix.Chat)

	// Convert messages to API format
	apiMessages := make([]map[string]interface{}, len(messages))
	for i, msg := range messages {
		apiMessages[i] = map[string]interface{}{
			"role":    msg.Role,
			"content": msg.Content,
		}
	}

	reqBody := map[string]interface{}{
		"model":       modelName,
		"messages":    apiMessages,
		"stream":      true,
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

	resp, err := o.baseModel.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 20*1024*1024))
		return fmt.Errorf("invalid status code: %d, body: %s", resp.StatusCode, "[redacted]")
	}

	// SSE parsing: read line by line
	sawTerminal, sawContent := false, false
	done, err := ParseSSEStream[map[string]interface{}](resp.Body, func(event map[string]interface{}) error {

		if err := ctx.Err(); err != nil {
			return err
		}
		if event == nil || event["error"] != nil {
			return fmt.Errorf("upstream stream error")
		}
		if code, ok := event["code"].(float64); ok && code != 0 {
			return fmt.Errorf("upstream stream error")
		}
		choices, ok := event["choices"].([]interface{})
		if !ok || len(choices) == 0 {
			return fmt.Errorf("invalid stream choices")
		}
		choice, ok := choices[0].(map[string]interface{})
		if !ok {
			return fmt.Errorf("invalid stream choice")
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

		if value, exists := delta["content"]; exists && value != nil {
			content, ok := value.(string)
			if !ok {
				return fmt.Errorf("invalid stream content")
			}
			if content != "" {
				sawContent = true
				if err := sender(&content, nil); err != nil {
					return err
				}
				if err := ctx.Err(); err != nil {
					return err
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
		return fmt.Errorf("stream ended before successful content completion")
	}

	endOfStream := "[DONE]"
	if err = sender(&endOfStream, nil); err != nil {
		return err
	}

	return nil
}

func (o *OrcaRouterModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	return nil, fmt.Errorf("%s no such method", o.Name())
}

func (o *OrcaRouterModel) Rerank(modelName *string, query string, documents []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return nil, fmt.Errorf("%s no such method", o.Name())
}

func (o *OrcaRouterModel) TranscribeAudio(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig) (*ASRResponse, error) {
	return nil, fmt.Errorf("%s no such method", o.Name())
}

func (o *OrcaRouterModel) TranscribeAudioWithSender(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s no such method", o.Name())
}

func (o *OrcaRouterModel) AudioSpeech(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig) (*TTSResponse, error) {
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if err := o.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if modelName == nil || *modelName == "" {
		return nil, fmt.Errorf("model name is required")
	}
	if ttsConfig != nil {
		for key := range ttsConfig.Params {
			switch key {
			case "model", "input", "stream", "api_key":
				return nil, fmt.Errorf("reserved speech parameter")
			}
		}
	}

	if audioContent == nil || *audioContent == "" {
		return nil, fmt.Errorf("audio content is empty")
	}

	resolvedBaseURL, err := o.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, o.baseModel.URLSuffix.TTS)

	reqBody := map[string]interface{}{
		"model":  *modelName,
		"input":  *audioContent,
		"stream": false,
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

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), longOpCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := o.baseModel.httpClient.Do(req)
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

	if len(body) == 0 || strings.Contains(strings.ToLower(resp.Header.Get("Content-Type")), "json") {
		return nil, fmt.Errorf("invalid speech response")
	}
	return &TTSResponse{Audio: body}, nil
}

func (o *OrcaRouterModel) AudioSpeechWithSender(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s no such method", o.Name())
}

func (o *OrcaRouterModel) OCRFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, ocrConfig *OCRConfig) (*OCRFileResponse, error) {
	return nil, fmt.Errorf("%s no such method", o.Name())
}

func (o *OrcaRouterModel) ParseFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*ParseFileResponse, error) {
	return nil, fmt.Errorf("%s no such method", o.Name())
}

func (o *OrcaRouterModel) ListModels(apiConfig *APIConfig) ([]ListModelResponse, error) {
	if err := o.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	resolvedBaseURL, err := o.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, o.baseModel.URLSuffix.Models)

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := o.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	// Parse response
	// Parse response
	var inventory map[string]json.RawMessage
	if err := json.Unmarshal(body, &inventory); err != nil || inventory == nil {
		return nil, fmt.Errorf("invalid model inventory")
	}
	if e, ok := inventory["error"]; ok && string(e) != "null" {
		return nil, fmt.Errorf("upstream inventory error")
	}
	data, ok := inventory["data"]
	if !ok || string(data) == "null" {
		return nil, fmt.Errorf("missing model inventory")
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

func (o *OrcaRouterModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("%s no such method", o.Name())
}

func (o *OrcaRouterModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := o.ListModels(apiConfig)
	return err
}

func (o *OrcaRouterModel) ListTasks(apiConfig *APIConfig) ([]ListTaskStatus, error) {
	return nil, fmt.Errorf("%s no such method", o.Name())
}

func (o *OrcaRouterModel) ShowTask(taskID string, apiConfig *APIConfig) (*TaskResponse, error) {
	return nil, fmt.Errorf("%s no such method", o.Name())
}
