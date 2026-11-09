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
	"math"
	"net/http"
	"strconv"
	"strings"
)

// DeepSeekModel implements ModelDriver for DeepSeek
type DeepSeekModel struct {
	baseModel BaseModel
}

// NewDeepSeekModel creates a new DeepSeek model instance
func NewDeepSeekModel(baseURL map[string]string, urlSuffix URLSuffix) *DeepSeekModel {
	if baseURL != nil {
		copied := make(map[string]string, len(baseURL))
		for region, value := range baseURL {
			copied[region] = value
		}
		baseURL = copied
	}
	return &DeepSeekModel{
		baseModel: BaseModel{
			BaseURL:    baseURL,
			URLSuffix:  urlSuffix,
			httpClient: NewDriverHTTPClient(),
		},
	}
}

func (d *DeepSeekModel) NewInstance(baseURL map[string]string) ModelDriver {
	return NewDeepSeekModel(baseURL, d.baseModel.URLSuffix)
}

func (d *DeepSeekModel) Name() string {
	return "deepseek"
}

func (d *DeepSeekModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	if chatModelConfig != nil && chatModelConfig.Stream != nil && *chatModelConfig.Stream {
		return nil, fmt.Errorf("stream must be false in ChatWithMessages")
	}
	if err := d.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if strings.TrimSpace(modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}
	if len(messages) == 0 {
		return nil, fmt.Errorf("messages is empty")
	}

	resolvedBaseURL, err := d.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", strings.TrimRight(resolvedBaseURL, "/"), strings.TrimLeft(d.baseModel.URLSuffix.Chat, "/"))

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
				var thinkingFlag string
				effort := "high"
				if chatModelConfig.Effort != nil {
					effort = *chatModelConfig.Effort
				}
				switch effort {
				case "none":
					thinkingFlag = "disabled"
					chatModelConfig.Thinking = nil
				case "low":
					thinkingFlag = "disabled"
					chatModelConfig.Thinking = nil
				case "medium":
					thinkingFlag = "disabled"
					chatModelConfig.Thinking = nil
				case "high":
					thinkingFlag = "enabled"
					reqBody["reasoning_effort"] = "high"
				case "default":
					thinkingFlag = "enabled"
					reqBody["reasoning_effort"] = "high"
				case "max":
					thinkingFlag = "enabled"
					reqBody["reasoning_effort"] = "max"
				default:
					thinkingFlag = "enabled"
					reqBody["reasoning_effort"] = effort
				}
				reqBody["thinking"] = map[string]interface{}{
					"type": thinkingFlag,
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

	resp, err := d.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 20*1024*1024+1))
	if err == nil && len(body) > 20*1024*1024 {
		err = fmt.Errorf("response exceeds limit")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("provider HTTP error: status %d", resp.StatusCode)
	}

	// Parse response
	var result map[string]interface{}

	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(body, &envelope); err != nil || envelope == nil {
		return nil, fmt.Errorf("invalid provider JSON response")
	}
	if raw, exists := envelope["error"]; exists && string(raw) != "null" {
		return nil, fmt.Errorf("provider response error")
	}
	if raw, exists := envelope["base_resp"]; exists {
		var status struct {
			Code int `json:"status_code"`
		}
		if json.Unmarshal(raw, &status) != nil || status.Code != 0 {
			return nil, fmt.Errorf("provider response error")
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
	if !ok || strings.TrimSpace(content) == "" {
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
func (d *DeepSeekModel) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("sender is required")
	}
	if chatModelConfig != nil && chatModelConfig.Stream != nil && !*chatModelConfig.Stream {
		return fmt.Errorf("stream must be true in ChatStreamlyWithSender")
	}
	if err := d.baseModel.APIConfigCheck(apiConfig); err != nil {
		return err
	}

	if len(messages) == 0 {
		return fmt.Errorf("messages is empty")
	}

	resolvedBaseURL, err := d.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return err
	}
	url := fmt.Sprintf("%s/chat/completions", resolvedBaseURL)

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
				var thinkingFlag string
				effort := "high"
				if chatModelConfig.Effort != nil {
					effort = *chatModelConfig.Effort
				}
				switch effort {
				case "none":
					thinkingFlag = "disabled"
					break
				case "low":
					thinkingFlag = "disabled"
					break
				case "medium":
					thinkingFlag = "disabled"
					break
				case "high":
					thinkingFlag = "enabled"
					reqBody["reasoning_effort"] = "high"
					break
				case "default":
					thinkingFlag = "enabled"
					reqBody["reasoning_effort"] = "high"
					break
				case "max":
					thinkingFlag = "enabled"
					reqBody["reasoning_effort"] = "max"
					break
				default:
					return fmt.Errorf("invalid effort level")
				}
				reqBody["thinking"] = map[string]interface{}{
					"type": thinkingFlag,
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

	resp, err := d.baseModel.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {

		return fmt.Errorf("provider HTTP error: status %d", resp.StatusCode)
	}

	// SSE parsing: read line by line
	sawTerminal := false
	sawAnswer := false
	done, err := ParseSSEStream[map[string]interface{}](resp.Body, func(event map[string]interface{}) error {

		if event["error"] != nil {
			return fmt.Errorf("provider stream error")
		}
		choices, ok := event["choices"].([]interface{})
		if !ok || len(choices) == 0 {
			if event["usage"] != nil {
				return nil
			}
			return fmt.Errorf("invalid stream choices")
		}

		firstChoice, ok := choices[0].(map[string]interface{})
		if !ok {
			return fmt.Errorf("invalid stream choice")
		}

		delta, ok := firstChoice["delta"].(map[string]interface{})
		if !ok {
			return fmt.Errorf("invalid stream delta")
		}

		content, ok := delta["content"].(string)
		if ok && content != "" {
			sawAnswer = true
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
			sawTerminal = true
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("failed to scan response body: %w", err)
	}
	if !done && !sawTerminal {
		return fmt.Errorf("deepseek: stream ended before [DONE] or finish_reason: %w", io.ErrUnexpectedEOF)
	}

	if !sawAnswer {
		return fmt.Errorf("stream returned no answer")
	}
	if err := apiConfig.RequestContext().Err(); err != nil {
		return err
	}
	// Send [DONE] marker for OpenAI compatibility
	endOfStream := "[DONE]"
	return sender(&endOfStream, nil)
}

// Embed embeds a list of texts into embeddings
func (d *DeepSeekModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	return nil, fmt.Errorf("%s, no such method", d.Name())
}

type DSModel struct {
	ID      string `json:"id"`
	Object  string `json:"object"`
	OwnedBy string `json:"owned_by"`
}

func (d *DeepSeekModel) ListModels(apiConfig *APIConfig) ([]ListModelResponse, error) {
	if err := d.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	resolvedBaseURL, err := d.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", strings.TrimRight(resolvedBaseURL, "/"), strings.TrimLeft(d.baseModel.URLSuffix.Models, "/"))

	// Build request body
	reqBody := map[string]interface{}{}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := d.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 20*1024*1024+1))
	if err == nil && len(body) > 20*1024*1024 {
		err = fmt.Errorf("response exceeds limit")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("provider HTTP error: status %d", resp.StatusCode)
	}

	// Parse response

	var envelope map[string]json.RawMessage
	if json.Unmarshal(body, &envelope) != nil || envelope == nil {
		return nil, fmt.Errorf("invalid models response")
	}
	if raw, ok := envelope["error"]; ok && string(raw) != "null" {
		return nil, fmt.Errorf("provider models error")
	}
	var modelRows []map[string]json.RawMessage
	if json.Unmarshal(envelope["data"], &modelRows) != nil || modelRows == nil {
		return nil, fmt.Errorf("invalid models data")
	}
	for _, row := range modelRows {
		var id string
		if json.Unmarshal(row["id"], &id) != nil || strings.TrimSpace(id) == "" {
			return nil, fmt.Errorf("invalid model id")
		}
	}
	var modelList ModelList
	if err = json.Unmarshal(body, &modelList); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	return ParseListModel(modelList), nil
}

// deepseekBalanceResponse is the shape returned by
// GET /user/balance. The balance fields are strings in the
// upstream API, so we parse them on our side.
type deepseekBalanceResponse struct {
	IsAvailable  bool `json:"is_available"`
	BalanceInfos []struct {
		Currency        string `json:"currency"`
		TotalBalance    string `json:"total_balance"`
		GrantedBalance  string `json:"granted_balance"`
		ToppedUpBalance string `json:"topped_up_balance"`
	} `json:"balance_infos"`
}

// Balance returns the user's available balance on DeepSeek by
// calling GET /user/balance with the configured Bearer token.
// The result map matches the shape used by the Moonshot driver,
// so the UI can render it without provider-specific code.
func (d *DeepSeekModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	if err := d.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	baseURL, err := d.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}

	url := fmt.Sprintf("%s/%s", strings.TrimRight(baseURL, "/"), strings.TrimLeft(d.baseModel.URLSuffix.Balance, "/"))

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := d.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 20*1024*1024+1))
	if err == nil && len(body) > 20*1024*1024 {
		err = fmt.Errorf("response exceeds limit")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("provider HTTP error: status %d", resp.StatusCode)
	}

	var envelope map[string]json.RawMessage
	if json.Unmarshal(body, &envelope) != nil || envelope == nil {
		return nil, fmt.Errorf("invalid balance response")
	}
	if raw, ok := envelope["error"]; ok && string(raw) != "null" {
		return nil, fmt.Errorf("provider balance error")
	}
	var parsed deepseekBalanceResponse
	if err = json.Unmarshal(body, &parsed); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	if len(parsed.BalanceInfos) == 0 {
		return nil, fmt.Errorf("no balance info in response")
	}

	// Pick the first balance entry, the same way the Moonshot
	// driver returns a single {balance, currency} pair to the UI.
	first := parsed.BalanceInfos[0]
	total, err := strconv.ParseFloat(first.TotalBalance, 64)
	if err != nil || math.IsNaN(total) || math.IsInf(total, 0) || strings.TrimSpace(first.Currency) == "" {
		return nil, fmt.Errorf("invalid balance entry")
	}

	return map[string]interface{}{
		"balance":  total,
		"currency": first.Currency,
	}, nil
}

func (d *DeepSeekModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := d.ListModels(apiConfig)
	if err != nil {
		return err
	}
	return nil
}

// Rerank calculates similarity scores between query and documents
func (d *DeepSeekModel) Rerank(modelName *string, query string, documents []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return nil, fmt.Errorf("%s, Rerank not implemented", d.Name())
}

// TranscribeAudio transcribe audio
func (d *DeepSeekModel) TranscribeAudio(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig) (*ASRResponse, error) {
	return nil, fmt.Errorf("%s, no such method", d.Name())
}

func (d *DeepSeekModel) TranscribeAudioWithSender(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", d.Name())
}

// AudioSpeech convert text to audio
func (d *DeepSeekModel) AudioSpeech(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig) (*TTSResponse, error) {
	return nil, fmt.Errorf("%s, no such method", d.Name())
}

func (d *DeepSeekModel) AudioSpeechWithSender(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", d.Name())
}

// OCRFile OCR file
func (d *DeepSeekModel) OCRFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, ocrConfig *OCRConfig) (*OCRFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", d.Name())
}

// ParseFile parse file
func (d *DeepSeekModel) ParseFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*ParseFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", d.Name())
}

func (d *DeepSeekModel) ListTasks(apiConfig *APIConfig) ([]ListTaskStatus, error) {
	return nil, fmt.Errorf("%s, no such method", d.Name())
}

func (d *DeepSeekModel) ShowTask(taskID string, apiConfig *APIConfig) (*TaskResponse, error) {
	return nil, fmt.Errorf("%s, no such method", d.Name())
}
