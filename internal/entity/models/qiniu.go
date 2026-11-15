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
	"fmt"
	json "github.com/goccy/go-json"
	"io"
	"net/http"
	"strings"
)

type QiniuModel struct {
	baseModel BaseModel
}

func NewQiniuModel(baseURL map[string]string, urlSuffix URLSuffix) *QiniuModel {
	if baseURL != nil {
		copied := make(map[string]string, len(baseURL))
		for region, value := range baseURL {
			copied[region] = value
		}
		baseURL = copied
	}
	return &QiniuModel{
		baseModel: BaseModel{
			BaseURL:    baseURL,
			URLSuffix:  urlSuffix,
			httpClient: NewDriverHTTPClient(),
		},
	}
}

func (q *QiniuModel) NewInstance(baseURL map[string]string) ModelDriver {
	return NewQiniuModel(baseURL, q.baseModel.URLSuffix)
}

func (q *QiniuModel) Name() string {
	return "qiniu"
}

var qiniuQwenThinkingModels = map[string]struct{}{
	"qwen3-next-80b-a3b-thinking":   {},
	"qwen3-235b-a22b-thinking-2507": {},
	"qwen3-max-2026-01-23":          {},
	"qwen-turbo":                    {},
	"qwen3-32b":                     {},
	"qwen3-30b-a3b":                 {},
	"qwen3-30b-a3b-thinking-2507":   {},
	"qwen3.5-397b-a17b":             {},
	"qwen/qwen3.6-plus":             {},
	"qwen/qwen3.7-max":              {},
	"qwen/qwen3.6-27b":              {},
	"qwen3.5-35b-a3b":               {},
	"qwen3-vl-30b-a3b-thinking":     {},
}

var qiniuThinkingModels = map[string]struct{}{
	"deepseek/deepseek-v4-flash":               {},
	"deepseek/deepseek-v4-pro":                 {},
	"moonshotai/kimi-k2.6":                     {},
	"z-ai/glm-5.1":                             {},
	"z-ai/glm-5":                               {},
	"minimax/minimax-m2.7":                     {},
	"minimax/minimax-m2.5":                     {},
	"minimax/minimax-m2.5-highspeed":           {},
	"kimi-k2-thinking":                         {},
	"z-ai/glm-4.6":                             {},
	"deepseek/deepseek-v3.2-251201":            {},
	"deepseek/deepseek-v3.2-exp-thinking":      {},
	"deepseek/deepseek-v3.1-terminus-thinking": {},
	"deepseek-r1-0528":                         {},
	"deepseek-r1":                              {},
	"doubao-seed-1.6-flash":                    {},
	"doubao-seed-1.6":                          {},
	"doubao-seed-2.0-pro":                      {},
	"doubao-seed-2.0-lite":                     {},
	"doubao-seed-2.0-mini":                     {},
	"doubao-seed-2.0-code":                     {},
	"minimax-m1":                               {},
	"glm-4.5":                                  {},
	"glm-4.5-air":                              {},
	"tencent/hy3-preview":                      {},
}

func applyQiniuThinkingConfig(reqBody map[string]interface{}, modelName string, chatModelConfig *ChatConfig) {
	if chatModelConfig == nil || chatModelConfig.Thinking == nil {
		return
	}

	lowerModelName := strings.ToLower(modelName)
	if _, ok := qiniuQwenThinkingModels[lowerModelName]; ok {
		enableThinking := *chatModelConfig.Thinking
		if chatModelConfig.Effort != nil && strings.ToLower(*chatModelConfig.Effort) == "none" {
			enableThinking = false
		}
		reqBody["enable_thinking"] = enableThinking
		return
	}

	if _, ok := qiniuThinkingModels[lowerModelName]; !ok {
		return
	}

	thinkingType := "disabled"
	if *chatModelConfig.Thinking {
		thinkingType = "enabled"
		effort := "high"
		if chatModelConfig.Effort != nil && *chatModelConfig.Effort != "" {
			effort = strings.ToLower(*chatModelConfig.Effort)
		}

		switch effort {
		case "none":
			thinkingType = "disabled"
		case "default":
			reqBody["reasoning_effort"] = "high"
		case "low", "medium", "high":
			reqBody["reasoning_effort"] = effort
		case "max":
			reqBody["reasoning_effort"] = "high"
		default:
			reqBody["reasoning_effort"] = effort
		}
	}

	reqBody["thinking"] = map[string]interface{}{
		"type": thinkingType,
	}
}

func (q *QiniuModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
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

	if err := q.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}
	if len(messages) == 0 {
		return nil, fmt.Errorf("no messages")
	}

	resolvedBaseURL, err := q.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, q.baseModel.URLSuffix.Chat)

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

		applyQiniuThinkingConfig(reqBody, modelName, chatModelConfig)
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

	resp, err := q.baseModel.httpClient.Do(req)
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

	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
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
		return nil, fmt.Errorf("invalid choice format")
	}

	messageMap, ok := firstChoice["message"].(map[string]interface{})
	if !ok {
		return nil, fmt.Errorf("invalid message format")
	}

	if value, exists := messageMap["reasoning_content"]; exists && value != nil {
		if _, ok := value.(string); !ok {
			return nil, fmt.Errorf("invalid reasoning content format")
		}
	}

	content, ok := messageMap["content"].(string)
	if !ok {
		return nil, fmt.Errorf("invalid content format")
	}

	var reasonContent string
	if chatModelConfig != nil && chatModelConfig.Thinking != nil && *chatModelConfig.Thinking {
		reasonContent, ok = messageMap["reasoning_content"].(string)
		if !ok {
			return nil, fmt.Errorf("invalid reasoning content format")
		}

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

func (q *QiniuModel) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig, sender func(*string, *string) error) error {
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
	if chatModelConfig != nil && chatModelConfig.MaxTokens != nil && *chatModelConfig.MaxTokens < 0 {
		return fmt.Errorf("max_tokens must be nonnegative")
	}
	if chatModelConfig != nil && chatModelConfig.Stream != nil && !*chatModelConfig.Stream {
		return fmt.Errorf("stream must be true")
	}

	if err := q.baseModel.APIConfigCheck(apiConfig); err != nil {
		return err
	}
	if len(messages) == 0 {
		return fmt.Errorf("messages is empty")
	}

	resolvedBaseURL, err := q.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return err
	}
	baseURL := strings.TrimSuffix(resolvedBaseURL, "/")
	url := fmt.Sprintf("%s/%s", baseURL, q.baseModel.URLSuffix.Chat)

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

		applyQiniuThinkingConfig(reqBody, modelName, chatModelConfig)
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

	resp, err := q.baseModel.httpClient.Do(req)
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
		for _, field := range []string{"reasoning_content"} {
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

func (q *QiniuModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	return nil, fmt.Errorf("%s, no such method", q.Name())
}

func (q *QiniuModel) Rerank(modelName *string, query string, documents []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return nil, fmt.Errorf("%s, no such method", q.Name())
}

func (q *QiniuModel) TranscribeAudio(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig) (*ASRResponse, error) {
	return nil, fmt.Errorf("%s, no such method", q.Name())
}

func (q *QiniuModel) TranscribeAudioWithSender(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", q.Name())
}

func (q *QiniuModel) AudioSpeech(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig) (*TTSResponse, error) {
	return nil, fmt.Errorf("%s, no such method", q.Name())
}

func (q *QiniuModel) AudioSpeechWithSender(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", q.Name())
}

func (q *QiniuModel) OCRFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, ocrConfig *OCRConfig) (*OCRFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", q.Name())
}

func (q *QiniuModel) ParseFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*ParseFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", q.Name())
}

func (q *QiniuModel) ListModels(apiConfig *APIConfig) ([]ListModelResponse, error) {
	if err := q.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	resolvedBaseURL, err := q.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL := strings.TrimSuffix(resolvedBaseURL, "/")
	url := fmt.Sprintf("%s/%s", baseURL, q.baseModel.URLSuffix.Models)

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := q.baseModel.httpClient.Do(req)
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

func (q *QiniuModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("%s, no such method", q.Name())
}

func (q *QiniuModel) CheckConnection(apiConfig *APIConfig) error {
	return fmt.Errorf("%s, no such method", q.Name())
}

func (q *QiniuModel) ListTasks(apiConfig *APIConfig) ([]ListTaskStatus, error) {
	return nil, fmt.Errorf("%s, no such method", q.Name())
}

func (q *QiniuModel) ShowTask(taskID string, apiConfig *APIConfig) (*TaskResponse, error) {
	return nil, fmt.Errorf("%s, no such method", q.Name())
}
