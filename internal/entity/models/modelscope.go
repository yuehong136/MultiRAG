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
	"sync"
	"time"
)

// modelscopeStreamIdleTimeout bounds how long a stream can go without
var modelscopeStreamIdleTimeout = 60 * time.Second

// ModelScopeModel implements ModelDriver for ModelScope chat models.
type ModelScopeModel struct {
	baseModel BaseModel
}

type modelscopeChatChoice struct {
	Message struct {
		Content          string `json:"content"`
		ReasoningContent string `json:"reasoning_content"`
		Reasoning        string `json:"reasoning"`
		Thinking         string `json:"thinking"`
	} `json:"message"`
}

type modelscopeChatResponse struct {
	Choices []modelscopeChatChoice `json:"choices"`
}

type modelscopeModelListResponse struct {
	Data []DSModel `json:"data"`
}

// NewModelScopeModel creates a new ModelScope model instance.
func NewModelScopeModel(baseURL map[string]string, urlSuffix URLSuffix) *ModelScopeModel {
	if baseURL != nil {
		copied := make(map[string]string, len(baseURL))
		for region, value := range baseURL {
			copied[region] = value
		}
		baseURL = copied
	}
	return &ModelScopeModel{
		baseModel: BaseModel{
			BaseURL:          baseURL,
			URLSuffix:        urlSuffix,
			AllowEmptyAPIKey: true,
			httpClient:       NewDriverHTTPClient(),
		},
	}
}

func (m *ModelScopeModel) NewInstance(baseURL map[string]string) ModelDriver {
	return NewModelScopeModel(baseURL, m.baseModel.URLSuffix)
}

func (m *ModelScopeModel) Name() string {
	return "modelscope"
}

func normalizeModelScopeBaseURL(base string) string {
	trimmed := strings.TrimRight(strings.TrimSpace(base), "/")
	if trimmed == "" {
		return trimmed
	}
	if strings.HasSuffix(trimmed, "/v1") {
		return strings.TrimSuffix(trimmed, "/v1")
	}
	return trimmed
}

func modelscopeReasoningFromStrings(reasoningContent string, reasoning string, thinking string) string {
	switch {
	case reasoningContent != "":
		return reasoningContent
	case reasoning != "":
		return reasoning
	case thinking != "":
		return thinking
	default:
		return ""
	}
}

func modelscopeReasoningFromMap(value map[string]interface{}) string {
	for _, field := range []string{"reasoning_content", "reasoning", "thinking"} {
		if text, ok := value[field].(string); ok && text != "" {
			return text
		}
	}
	return ""
}

func buildModelScopeChatBody(modelName string, messages []Message, stream bool, chatModelConfig *ChatConfig) map[string]interface{} {
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
	}

	return reqBody
}

// ChatWithMessages sends multiple messages with roles and returns the response.
func (m *ModelScopeModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
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

	if err := m.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(messages) == 0 {
		return nil, fmt.Errorf("messages is empty")
	}

	baseURL, err := m.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL = normalizeModelScopeBaseURL(baseURL)
	url := fmt.Sprintf("%s/%s", baseURL, m.baseModel.URLSuffix.Chat)

	reqBody := buildModelScopeChatBody(modelName, messages, false, chatModelConfig)
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
	if auth := BearerAuth(apiConfig); auth != "" {
		req.Header.Set("Authorization", auth)
	}

	resp, err := m.baseModel.httpClient.Do(req)
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

	var raw map[string]any

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
	if err := json.Unmarshal(body, &raw); err != nil {
		return nil, fmt.Errorf("invalid chat response")
	}
	if raw == nil || raw["error"] != nil {
		return nil, fmt.Errorf("upstream chat error")
	}
	if code, ok := raw["code"].(float64); ok && code != 0 {
		return nil, fmt.Errorf("upstream chat error")
	}
	choices, ok := raw["choices"].([]any)
	if !ok || len(choices) == 0 {
		return nil, fmt.Errorf("no choices in response")
	}
	choice, ok := choices[0].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("invalid choice")
	}
	message, ok := choice["message"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("invalid message")
	}
	if _, ok := message["content"].(string); !ok {
		return nil, fmt.Errorf("invalid content")
	}

	var result modelscopeChatResponse
	if err = json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}
	if len(result.Choices) == 0 {
		return nil, fmt.Errorf("no choices in response")
	}

	content := result.Choices[0].Message.Content
	reasonContent := modelscopeReasoningFromStrings(
		result.Choices[0].Message.ReasoningContent,
		result.Choices[0].Message.Reasoning,
		result.Choices[0].Message.Thinking,
	)

	return &ChatResponse{
		Answer:        &content,
		ReasonContent: &reasonContent,
	}, nil
}

// ChatStreamlyWithSender sends messages and streams response via sender.
func (m *ModelScopeModel) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	idleTimeout := modelscopeStreamIdleTimeout

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
	if chatModelConfig != nil && chatModelConfig.MaxTokens != nil && *chatModelConfig.MaxTokens < 0 {
		return fmt.Errorf("max_tokens must be nonnegative")
	}
	if chatModelConfig != nil && chatModelConfig.Stream != nil && !*chatModelConfig.Stream {
		return fmt.Errorf("stream must be true")
	}

	if err := m.baseModel.APIConfigCheck(apiConfig); err != nil {
		return err
	}

	if sender == nil {
		return fmt.Errorf("sender is required")
	}
	if len(messages) == 0 {
		return fmt.Errorf("messages is empty")
	}
	if chatModelConfig != nil && chatModelConfig.Stream != nil && !*chatModelConfig.Stream {
		return fmt.Errorf("stream must be true in ChatStreamlyWithSender")
	}

	baseURL, err := m.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return err
	}
	baseURL = normalizeModelScopeBaseURL(baseURL)
	url := fmt.Sprintf("%s/%s", baseURL, m.baseModel.URLSuffix.Chat)

	reqBody := buildModelScopeChatBody(modelName, messages, true, chatModelConfig)
	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return fmt.Errorf("failed to marshal request: %w", err)
	}

	ctx, cancel := context.WithCancel(apiConfig.RequestContext())
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if auth := BearerAuth(apiConfig); auth != "" {
		req.Header.Set("Authorization", auth)
	}

	resp, err := m.baseModel.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 20*1024*1024))
		return fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	lastActive := time.Now()
	var lastActiveMu sync.Mutex
	done := make(chan struct{})
	defer close(done)
	go func() {
		ticker := time.NewTicker(idleTimeout / 4)
		defer ticker.Stop()
		for {
			select {
			case <-done:
				return
			case now := <-ticker.C:
				lastActiveMu.Lock()
				idle := now.Sub(lastActive)
				lastActiveMu.Unlock()
				if idle >= idleTimeout {
					cancel()
					return
				}
			}
		}
	}()

	sawTerminal, sawContent := false, false
	streamDone, err := ParseSSEStream[map[string]interface{}](resp.Body, func(event map[string]interface{}) error {
		lastActiveMu.Lock()
		lastActive = time.Now()
		lastActiveMu.Unlock()
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
		for _, field := range []string{"reasoning_content", "reasoning", "thinking"} {
			if value, exists := delta[field]; exists && value != nil {
				text, ok := value.(string)
				if !ok {
					return fmt.Errorf("invalid reasoning content")
				}
				if text != "" {
					sawContent = true
					if err := sender(nil, &text); err != nil {
						return err
					}
					if err := ctx.Err(); err != nil {
						return err
					}
				}
			}
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
	if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
		return parentErr
	}
	if ctx.Err() != nil {
		return fmt.Errorf("modelscope: stream idle timeout after %s", idleTimeout)
	}
	if err != nil {
		return fmt.Errorf("failed to scan response body: %w", err)
	}
	if (!streamDone && !sawTerminal) || !sawContent {
		return fmt.Errorf("stream ended before successful content completion")
	}

	endOfStream := "[DONE]"
	return sender(&endOfStream, nil)
}

func (m *ModelScopeModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	return nil, fmt.Errorf("%s, no such method", m.Name())
}

func (m *ModelScopeModel) Rerank(modelName *string, query string, documents []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return nil, fmt.Errorf("%s, no such method", m.Name())
}

func (m *ModelScopeModel) TranscribeAudio(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig) (*ASRResponse, error) {
	return nil, fmt.Errorf("%s, no such method", m.Name())
}

func (m *ModelScopeModel) TranscribeAudioWithSender(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", m.Name())
}

func (m *ModelScopeModel) AudioSpeech(modelName *string, audioContent *string, apiConfig *APIConfig, asrConfig *TTSConfig) (*TTSResponse, error) {
	return nil, fmt.Errorf("%s, no such method", m.Name())
}

func (m *ModelScopeModel) AudioSpeechWithSender(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", m.Name())
}

func (m *ModelScopeModel) OCRFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, ocrConfig *OCRConfig) (*OCRFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", m.Name())
}

func (m *ModelScopeModel) ParseFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*ParseFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", m.Name())
}

// ListModels returns the model IDs exposed by ModelScope's OpenAI-compatible
// /v1/models endpoint.
func (m *ModelScopeModel) ListModels(apiConfig *APIConfig) ([]ListModelResponse, error) {
	if err := m.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	baseURL, err := m.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL = normalizeModelScopeBaseURL(baseURL)
	url := fmt.Sprintf("%s/%s", baseURL, m.baseModel.URLSuffix.Models)

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	if auth := BearerAuth(apiConfig); auth != "" {
		req.Header.Set("Authorization", auth)
	}

	resp, err := m.baseModel.httpClient.Do(req)
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

	var result modelscopeModelListResponse
	if err = json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	return ParseListModel(ModelList{Models: result.Data}), nil
}

func (m *ModelScopeModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("%s, no such method", m.Name())
}

func (m *ModelScopeModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := m.ListModels(apiConfig)
	return err
}

func (m *ModelScopeModel) ListTasks(apiConfig *APIConfig) ([]ListTaskStatus, error) {
	return nil, fmt.Errorf("%s, no such method", m.Name())
}

func (m *ModelScopeModel) ShowTask(taskID string, apiConfig *APIConfig) (*TaskResponse, error) {
	return nil, fmt.Errorf("%s, no such method", m.Name())
}
