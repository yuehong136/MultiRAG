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
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type AI302Model struct {
	baseModel BaseModel
}

func NewAI302Model(baseURL map[string]string, urlSuffix URLSuffix) *AI302Model {
	if baseURL != nil {
		copied := make(map[string]string, len(baseURL))
		for region, value := range baseURL {
			copied[region] = value
		}
		baseURL = copied
	}
	return &AI302Model{
		baseModel: BaseModel{
			BaseURL:    baseURL,
			URLSuffix:  urlSuffix,
			httpClient: NewDriverHTTPClient(),
		},
	}
}

func (a *AI302Model) NewInstance(baseURL map[string]string) ModelDriver {
	return NewAI302Model(baseURL, a.baseModel.URLSuffix)
}

func (a *AI302Model) Name() string {
	return "302ai"
}

func validateAI302ModelName(modelName *string) (string, error) {
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return "", fmt.Errorf("model name is required")
	}
	return strings.TrimSpace(*modelName), nil
}

func validateAI302DocumentURL(rawURL string) (string, error) {
	documentURL := strings.TrimSpace(rawURL)
	parsedURL, err := url.Parse(documentURL)
	if err != nil || parsedURL.Host == "" || parsedURL.User != nil || parsedURL.Fragment != "" || (parsedURL.Scheme != "http" && parsedURL.Scheme != "https") {
		return "", fmt.Errorf("invalid document URL")
	}
	return documentURL, nil
}

func (a *AI302Model) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	if strings.Trim(strings.TrimSpace(a.baseModel.URLSuffix.Chat), "/") == "" {
		return nil, fmt.Errorf("provider endpoint is not configured")
	}

	if err := a.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}
	apiKey := strings.TrimSpace(*apiConfig.ApiKey)
	if strings.TrimSpace(modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}
	if len(messages) == 0 {
		return nil, fmt.Errorf("messages is empty")
	}

	resolvedBaseURL, err := a.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, a.baseModel.URLSuffix.Chat)

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
		"model":       strings.TrimSpace(modelName),
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

		if chatModelConfig.Effort != nil {
			reqBody["reasoning"] = map[string]interface{}{
				"effort": *chatModelConfig.Effort,
			}
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
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", apiKey))

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
	var result map[string]interface{}
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

func (a *AI302Model) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, modelConfig *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	if strings.Trim(strings.TrimSpace(a.baseModel.URLSuffix.Chat), "/") == "" {
		return fmt.Errorf("provider endpoint is not configured")
	}

	if err := a.baseModel.APIConfigCheck(apiConfig); err != nil {
		return err
	}
	apiKey := strings.TrimSpace(*apiConfig.ApiKey)
	if strings.TrimSpace(modelName) == "" {
		return fmt.Errorf("model name is required")
	}
	if sender == nil {
		return fmt.Errorf("sender is required")
	}
	if len(messages) == 0 {
		return fmt.Errorf("messages is empty")
	}

	resolvedBaseURL, err := a.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return err
	}
	url := fmt.Sprintf("%s/%s", strings.TrimSuffix(resolvedBaseURL, "/"), a.baseModel.URLSuffix.Chat)

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
		"model":       strings.TrimSpace(modelName),
		"messages":    apiMessages,
		"stream":      true,
		"temperature": 1,
	}

	if modelConfig != nil {
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

		if modelConfig.Effort != nil {
			reqBody["reasoning"] = map[string]interface{}{
				"effort": *modelConfig.Effort,
			}
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

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), streamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", apiKey))
	req.Header.Set("Accept", "text/event-stream")

	resp, err := a.baseModel.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	seenContent, finished := false, false
	done, err := ParseSSEStream[map[string]interface{}](resp.Body, func(event map[string]interface{}) error {
		if event == nil {
			return fmt.Errorf("empty stream event")
		}
		if failure, exists := event["error"]; exists && failure != nil {
			return fmt.Errorf("provider stream failed")
		}
		choices, ok := event["choices"].([]interface{})
		if !ok || len(choices) == 0 {
			return fmt.Errorf("stream event has no choices")
		}
		first, ok := choices[0].(map[string]interface{})
		if !ok {
			return fmt.Errorf("invalid stream choice")
		}
		if reason, exists := first["finish_reason"]; exists && reason != nil {
			value, ok := reason.(string)
			if !ok || (value != "" && value != "stop" && value != "length") {
				return fmt.Errorf("invalid stream terminal")
			}
			finished = finished || value != ""
		}
		delta, ok := first["delta"].(map[string]interface{})
		if !ok {
			if !finished || first["delta"] != nil {
				return fmt.Errorf("invalid stream delta")
			}
			delta = map[string]interface{}{}
		}
		if value, exists := delta["tool_calls"]; exists && value != nil {
			return fmt.Errorf("tool calls are not supported")
		}
		if value, exists := delta["reasoning_content"]; exists && value != nil {
			text, ok := value.(string)
			if !ok {
				return fmt.Errorf("invalid reasoning delta")
			}
			if text != "" {
				if err := sender(nil, &text); err != nil {
					return err
				}
			}
		}
		if value, exists := delta["content"]; exists && value != nil {
			text, ok := value.(string)
			if !ok {
				return fmt.Errorf("invalid content delta")
			}
			if text != "" {
				seenContent = true
				if err := sender(&text, nil); err != nil {
					return err
				}
			}
		}
		return nil
	})
	if ctx.Err() != nil {
		return ctx.Err()
	}
	if err != nil {
		return fmt.Errorf("failed to scan response body: %w", err)
	}
	if !seenContent || (!done && !finished) {
		return fmt.Errorf("incomplete chat stream")
	}

	// Send [DONE] marker for OpenAI compatibility
	endOfStream := "[DONE]"
	return sender(&endOfStream, nil)
}

func (a *AI302Model) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	if strings.Trim(strings.TrimSpace(a.baseModel.URLSuffix.Embedding), "/") == "" {
		return nil, fmt.Errorf("provider endpoint is not configured")
	}

	if err := a.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}
	if len(texts) == 0 {
		return []EmbeddingData{}, nil
	}
	model, err := validateAI302ModelName(modelName)
	if err != nil {
		return nil, err
	}
	apiKey := strings.TrimSpace(*apiConfig.ApiKey)

	resolvedBaseURL, err := a.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, a.baseModel.URLSuffix.Embedding)

	reqBody := map[string]interface{}{
		"model": model,
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
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", apiKey))

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
		return nil, fmt.Errorf("Jina embedding API error: status %d, body: %s", resp.StatusCode, "[redacted]")
	}

	var parsedResponse struct {
		Data []EmbeddingData `json:"data"`
	}

	if err = json.Unmarshal(body, &parsedResponse); err != nil {
		return nil, fmt.Errorf("failed to decode response: %w", err)
	}

	if len(parsedResponse.Data) == 0 {
		return nil, fmt.Errorf("Jina embedding response contains no data: %s", "[redacted]")
	}

	if len(parsedResponse.Data) != len(texts) {
		return nil, fmt.Errorf("embedding cardinality mismatch")
	}
	seen := make(map[int]bool)
	dimension := 0
	for _, row := range parsedResponse.Data {
		if row.Index < 0 || row.Index >= len(texts) || seen[row.Index] {
			return nil, fmt.Errorf("invalid embedding index")
		}
		seen[row.Index] = true
		if dimension == 0 {
			dimension = len(row.Embedding)
		}
		if len(row.Embedding) != dimension {
			return nil, fmt.Errorf("inconsistent embedding dimensions")
		}
	}
	embeddings := parsedResponse.Data

	return embeddings, nil
}

func (a *AI302Model) Rerank(modelName *string, query string, documents []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
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

	if strings.Trim(strings.TrimSpace(a.baseModel.URLSuffix.Rerank), "/") == "" {
		return nil, fmt.Errorf("provider endpoint is not configured")
	}

	if err := a.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(documents) == 0 {
		return &RerankResponse{}, nil
	}
	if strings.TrimSpace(query) == "" {
		return nil, fmt.Errorf("query is required")
	}
	model, err := validateAI302ModelName(modelName)
	if err != nil {
		return nil, err
	}
	apiKey := strings.TrimSpace(*apiConfig.ApiKey)

	resolvedBaseURL, err := a.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, a.baseModel.URLSuffix.Rerank)

	var topN int
	if rerankConfig != nil && rerankConfig.TopN < 0 {
		return nil, fmt.Errorf("top_n must not be negative")
	}
	if rerankConfig != nil && rerankConfig.TopN != 0 {
		topN = rerankConfig.TopN
	}

	reqBody := map[string]interface{}{
		"model":     model,
		"query":     strings.TrimSpace(query),
		"documents": documents,
		"top_n":     topN,
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
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", apiKey))

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
		return nil, fmt.Errorf("302.ai Rerank API error: status %d, body: %s", resp.StatusCode, "[redacted]")
	}

	var rerankResp struct {
		Results []RerankResult `json:"results"`
	}

	if err = json.Unmarshal(body, &rerankResp); err != nil {
		return nil, fmt.Errorf("failed to decode response: %w", err)
	}

	expected := len(documents)
	if topN > 0 && topN < expected {
		expected = topN
	}
	if len(rerankResp.Results) != expected {
		return nil, fmt.Errorf("rerank cardinality mismatch")
	}
	seen := make(map[int]bool)
	for _, row := range rerankResp.Results {
		if row.Index >= len(documents) || seen[row.Index] {
			return nil, fmt.Errorf("invalid rerank index")
		}
		seen[row.Index] = true
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

func (a *AI302Model) TranscribeAudio(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig) (*ASRResponse, error) {
	if strings.Trim(strings.TrimSpace(a.baseModel.URLSuffix.ASR), "/") == "" {
		return nil, fmt.Errorf("provider endpoint is not configured")
	}

	if err := a.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if file == nil || strings.TrimSpace(*file) == "" {
		return nil, fmt.Errorf("file is missing")
	}
	model, err := validateAI302ModelName(modelName)
	if err != nil {
		return nil, err
	}
	apiKey := strings.TrimSpace(*apiConfig.ApiKey)

	resolvedBaseURL, err := a.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, a.baseModel.URLSuffix.ASR)

	// multipart body
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)

	// open audio file
	audioFile, err := os.Open(strings.TrimSpace(*file))
	if err != nil {
		return nil, fmt.Errorf("failed to open audio file: %w", err)
	}
	defer audioFile.Close()

	// create multipart file field
	part, err := writer.CreateFormFile(
		"file",
		filepath.Base(strings.TrimSpace(*file)),
	)
	if err != nil {
		return nil, fmt.Errorf("failed to create multipart file: %w", err)
	}

	// copy file content
	if _, err = io.Copy(part, audioFile); err != nil {
		return nil, fmt.Errorf("failed to copy audio data: %w", err)
	}

	// model field
	if err := writer.WriteField("model", model); err != nil {
		return nil, fmt.Errorf("failed to write model field: %w", err)
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
			case int64:
				val = strconv.FormatInt(v, 10)
			case float32:
				val = strconv.FormatFloat(float64(v), 'f', -1, 32)
			case float64:
				val = strconv.FormatFloat(v, 'f', -1, 64)
			default:
				val = fmt.Sprintf("%v", v)
			}

			if err = writer.WriteField(key, val); err != nil {
				return nil, fmt.Errorf("failed to write field %s: %w", key, err)
			}
		}
	}

	if err = writer.Close(); err != nil {
		return nil, fmt.Errorf("failed to close multipart writer: %w", err)
	}

	// build request
	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), longOpCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "POST", url, &body)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", apiKey))
	req.Header.Set("Content-Type", writer.FormDataContentType())
	req.Header.Set("Accept", "application/json")

	// send request
	resp, err := a.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("302.ai ASR error: %s - %s", resp.Status, "[redacted]")
	}

	// Response
	var result struct {
		Text *string `json:"text"`
	}

	if err = json.Unmarshal(respBody, &result); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response: %w, body=%s", err, "[redacted]")
	}

	if result.Text == nil {
		return nil, fmt.Errorf("ASR response has no text field")
	}
	return &ASRResponse{Text: *result.Text}, nil
}

func (a *AI302Model) TranscribeAudioWithSender(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s no such method", a.Name())
}

func (a *AI302Model) AudioSpeech(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig) (*TTSResponse, error) {
	// TODO https://302ai-en.apifox.cn/225254060e0
	return nil, fmt.Errorf("%s no such method", a.Name())
}

func (a *AI302Model) AudioSpeechWithSender(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s no such method", a.Name())
}

func (a *AI302Model) OCRFile(modelName *string, content []byte, urls *string, apiConfig *APIConfig, ocrConfig *OCRConfig) (*OCRFileResponse, error) {
	if strings.Trim(strings.TrimSpace(a.baseModel.URLSuffix.OCR), "/") == "" {
		return nil, fmt.Errorf("provider endpoint is not configured")
	}

	if err := a.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if (urls == nil || strings.TrimSpace(*urls) == "") && (content == nil || len(content) == 0) {
		return nil, fmt.Errorf("file url or content is required")
	}
	model, err := validateAI302ModelName(modelName)
	if err != nil {
		return nil, err
	}
	apiKey := strings.TrimSpace(*apiConfig.ApiKey)

	resolvedBaseURL, err := a.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, a.baseModel.URLSuffix.OCR)

	var docURL string
	if urls != nil && strings.TrimSpace(*urls) != "" {
		docURL, err = validateAI302DocumentURL(*urls)
		if err != nil {
			return nil, err
		}
	} else {
		mimeType := http.DetectContentType(content)
		base64Str := base64.StdEncoding.EncodeToString(content)
		docURL = fmt.Sprintf("data:%s;base64,%s", mimeType, base64Str)
	}

	reqData := map[string]interface{}{
		"model": model,
		"document": map[string]interface{}{
			"type":         "document_url",
			"document_url": docURL,
		},
	}

	jsonData, err := json.Marshal(reqData)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal json payload: %w", err)
	}

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), longOpCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", apiKey))

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
		return nil, fmt.Errorf("Mistral OCR API error: %s, body: %s", resp.Status, "[redacted]")
	}

	var mistralResp struct {
		Pages []struct {
			Index    *int    `json:"index"`
			Markdown *string `json:"markdown"`
		} `json:"pages"`
	}

	if err = json.Unmarshal(body, &mistralResp); err != nil {
		return nil, fmt.Errorf("failed to parse response json: %w", err)
	}

	if len(mistralResp.Pages) == 0 {
		return nil, fmt.Errorf("OCR response has no pages")
	}
	seen := make(map[int]bool)
	for _, page := range mistralResp.Pages {
		if page.Index == nil || *page.Index < 0 || page.Markdown == nil || seen[*page.Index] {
			return nil, fmt.Errorf("invalid OCR page")
		}
		seen[*page.Index] = true
	}

	var fullMarkdown strings.Builder
	for _, page := range mistralResp.Pages {
		fullMarkdown.WriteString(*page.Markdown)
		fullMarkdown.WriteString("\n\n")
	}

	resultText := strings.TrimSpace(fullMarkdown.String())

	return &OCRFileResponse{
		Text: &resultText,
	}, nil
}

func (a *AI302Model) ParseFile(modelName *string, content []byte, documentURL *string, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*ParseFileResponse, error) {
	if strings.Trim(strings.TrimSpace(a.baseModel.URLSuffix.DocumentParse), "/") == "" {
		return nil, fmt.Errorf("provider endpoint is not configured")
	}

	if err := a.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if documentURL == nil || strings.TrimSpace(*documentURL) == "" {
		return nil, fmt.Errorf("302.ai API requires a valid public document URL; direct file upload is not supported")
	}
	docURL, err := validateAI302DocumentURL(*documentURL)
	if err != nil {
		return nil, err
	}

	resolvedBaseURL, err := a.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	apiURL := fmt.Sprintf("%s/%s", resolvedBaseURL, a.baseModel.URLSuffix.DocumentParse)

	reqBody := map[string]interface{}{
		"url": docURL,
	}

	if modelName != nil && strings.TrimSpace(*modelName) != "" {
		reqBody["model_version"] = strings.TrimSpace(*modelName)
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), longOpCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "POST", apiURL, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", strings.TrimSpace(*apiConfig.ApiKey)))

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
		return nil, fmt.Errorf("302.ai API failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	var taskResp mineruTaskSubmitResponse
	if err := json.Unmarshal(body, &taskResp); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	if taskResp.Code == nil || *taskResp.Code != 0 || (len(taskResp.Error) > 0 && string(taskResp.Error) != "null") {
		return nil, fmt.Errorf("302.ai task submission failed")
	}
	id := taskResp.Data.TaskID
	if strings.TrimSpace(id) == "" || strings.ContainsAny(id, "/\\?#") || id == "." || id == ".." {
		return nil, fmt.Errorf("invalid task ID")
	}

	return &ParseFileResponse{
		TaskID: taskResp.Data.TaskID,
	}, nil
}

func (a *AI302Model) ListModels(apiConfig *APIConfig) ([]ListModelResponse, error) {
	if strings.Trim(strings.TrimSpace(a.baseModel.URLSuffix.Models), "/") == "" {
		return nil, fmt.Errorf("provider endpoint is not configured")
	}

	if err := a.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}
	apiKey := strings.TrimSpace(*apiConfig.ApiKey)

	resolvedBaseURL, err := a.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, a.baseModel.URLSuffix.Models)

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", apiKey))
	req.Header.Set("Accept", "application/json")

	resp, err := a.baseModel.httpClient.Do(req)
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

	var modelList ModelList
	if err = json.Unmarshal(body, &modelList); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}
	if modelList.Models == nil {
		return nil, fmt.Errorf("invalid models list format")
	}
	models := ParseListModel(modelList)
	if len(models) == 0 {
		return nil, fmt.Errorf("invalid models list format")
	}
	return models, nil
}

func (a *AI302Model) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("%s no such method", a.Name())
}

func (a *AI302Model) CheckConnection(apiConfig *APIConfig) error {
	_, err := a.ListModels(apiConfig)
	return err
}

func (a *AI302Model) ListTasks(apiConfig *APIConfig) ([]ListTaskStatus, error) {
	return nil, fmt.Errorf("%s no such method", a.Name())
}

func (a *AI302Model) ShowTask(taskID string, apiConfig *APIConfig) (*TaskResponse, error) {
	if strings.Trim(strings.TrimSpace(a.baseModel.URLSuffix.DocumentParse), "/") == "" {
		return nil, fmt.Errorf("provider endpoint is not configured")
	}

	if err := a.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if strings.TrimSpace(taskID) == "" {
		return nil, fmt.Errorf("task id is required")
	}
	if strings.ContainsAny(taskID, "/\\?#") || taskID == "." || taskID == ".." {
		return nil, fmt.Errorf("invalid task ID")
	}

	// URL: https://mineru.net/api/v4/extract/task/{task_id}
	resolvedBaseURL, err := a.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	apiURL := fmt.Sprintf("%s/%s/%s", resolvedBaseURL, a.baseModel.URLSuffix.DocumentParse, url.PathEscape(strings.TrimSpace(taskID)))

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", apiURL, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", strings.TrimSpace(*apiConfig.ApiKey)))

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
		return nil, fmt.Errorf("MinerU query API failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	var queryResp mineruTaskQueryResponse
	if err := json.Unmarshal(body, &queryResp); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	if queryResp.Code == nil || *queryResp.Code != 0 || (len(queryResp.Error) > 0 && string(queryResp.Error) != "null") {
		return nil, fmt.Errorf("302.ai task query failed")
	}
	switch queryResp.Data.State {
	case "pending", "running", "converting", "done":
	case "failed":
		return nil, fmt.Errorf("302.ai task failed")
	default:
		return nil, fmt.Errorf("invalid task state")
	}
	if queryResp.Data.State == "done" {
		parsed, err := url.Parse(queryResp.Data.FullZipURL)
		if err != nil || parsed.Hostname() == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.User != nil || parsed.Fragment != "" {
			return nil, fmt.Errorf("invalid task result URL")
		}
	}

	// failed state
	if queryResp.Data.State == "failed" {
		return nil, fmt.Errorf("MinerU task failed: %s", queryResp.Data.ErrMsg)
	}

	if queryResp.Data.State != "done" {
		return nil, fmt.Errorf("task has no document result yet")
	}
	content := queryResp.Data.FullZipURL
	if strings.TrimSpace(content) == "" {
		return nil, fmt.Errorf("task result URL is missing")
	}

	return &TaskResponse{
		Segments: []TaskSegment{
			{
				Index:   0,
				Content: content,
			},
		},
	}, nil
}
