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
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// OpenAIModel implements ModelDriver for OpenAI (GPT models).
type OpenAIModel struct {
	baseModel BaseModel
}

// NewOpenAIModel creates a new OpenAI model instance.
func NewOpenAIModel(baseURL map[string]string, urlSuffix URLSuffix) *OpenAIModel {
	if baseURL != nil {
		copied := make(map[string]string, len(baseURL))
		for region, value := range baseURL {
			copied[region] = value
		}
		baseURL = copied
	}
	return &OpenAIModel{
		baseModel: BaseModel{
			BaseURL:    baseURL,
			URLSuffix:  urlSuffix,
			httpClient: NewDriverHTTPClient(),
		},
	}
}

func (o *OpenAIModel) NewInstance(baseURL map[string]string) ModelDriver {
	return NewOpenAIModel(baseURL, o.baseModel.URLSuffix)
}

func (o *OpenAIModel) Name() string {
	return "openai"
}

// ChatWithMessages sends multiple messages with roles and returns the response
func (o *OpenAIModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	if chatModelConfig != nil && chatModelConfig.Effort != nil {
		return nil, fmt.Errorf("OpenAI reasoning effort configuration is not implemented")
	}
	if chatModelConfig != nil && chatModelConfig.Stream != nil && *chatModelConfig.Stream {
		return nil, fmt.Errorf("stream must be false in ChatWithMessages")
	}
	if err := o.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if strings.TrimSpace(modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}
	if len(messages) == 0 {
		return nil, fmt.Errorf("messages is empty")
	}

	baseURL, err := o.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL = strings.TrimSuffix(baseURL, "/")
	url := fmt.Sprintf("%s/%s", strings.TrimRight(baseURL, "/"), strings.TrimLeft(o.baseModel.URLSuffix.Chat, "/"))

	// Convert messages to the format expected by the API
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

	resp, err := o.baseModel.httpClient.Do(req)
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

	// OpenAI reasoning models (o-series and similar) return reasoning text in
	// the reasoning_content field. Pass it through when present.
	var reasonContent string
	if rc, ok := messageMap["reasoning_content"].(string); ok {
		reasonContent = rc
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

// ChatStreamlyWithSender sends messages and streams the response
func (o *OpenAIModel) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig, sender func(*string, *string) error) error {
	if chatModelConfig != nil && chatModelConfig.Effort != nil {
		return fmt.Errorf("OpenAI reasoning effort configuration is not implemented")
	}
	if sender == nil {
		return fmt.Errorf("sender is required")
	}
	if chatModelConfig != nil && chatModelConfig.Stream != nil && !*chatModelConfig.Stream {
		return fmt.Errorf("stream must be true in ChatStreamlyWithSender")
	}
	if err := o.baseModel.APIConfigCheck(apiConfig); err != nil {
		return err
	}

	if len(messages) == 0 {
		return fmt.Errorf("messages is empty")
	}

	baseURL, err := o.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return err
	}
	baseURL = strings.TrimSuffix(baseURL, "/")
	url := fmt.Sprintf("%s/%s", strings.TrimRight(baseURL, "/"), strings.TrimLeft(o.baseModel.URLSuffix.Chat, "/"))

	// Convert messages to API format (supports multimodal content)
	apiMessages := make([]map[string]interface{}, len(messages))
	for i, msg := range messages {
		apiMessages[i] = map[string]interface{}{
			"role":    msg.Role,
			"content": msg.Content,
		}
	}

	// Build request body with streaming on by default
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

		return fmt.Errorf("provider HTTP error: status %d", resp.StatusCode)
	}

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

		reasoningContent, ok := delta["reasoning_content"].(string)
		if ok && reasoningContent != "" {
			if err := sender(nil, &reasoningContent); err != nil {
				return err
			}
		}

		content, ok := delta["content"].(string)
		if ok && content != "" {
			sawAnswer = true
			if err := sender(&content, nil); err != nil {
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
		return fmt.Errorf("openai: stream ended before [DONE] or finish_reason: %w", io.ErrUnexpectedEOF)
	}

	if !sawAnswer {
		return fmt.Errorf("stream returned no answer")
	}
	if err := apiConfig.RequestContext().Err(); err != nil {
		return err
	}
	// Send the [DONE] marker for OpenAI compatibility
	endOfStream := "[DONE]"
	if err := sender(&endOfStream, nil); err != nil {
		return err
	}

	return nil
}

type openaiEmbeddingResponse struct {
	Data   []openrouterEmbeddingData `json:"data"`
	Model  string                    `json:"model"`
	Object string                    `json:"object"`
	Usage  openrouterUsage           `json:"usage"`
}

type openaiEmbeddingData struct {
	Embedding []float64 `json:"embedding"`
	Object    string    `json:"object"`
	Index     int       `json:"index"`
}

type openaiUsage struct {
	PromptTokens int `json:"prompt_tokens"`
	TotalTokens  int `json:"total_tokens"`
}

func (o *OpenAIModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	if err := o.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(texts) == 0 {
		return []EmbeddingData{}, nil
	}

	if modelName == nil || *modelName == "" {
		return nil, fmt.Errorf("model name is required")
	}

	baseURL, err := o.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL = strings.TrimSuffix(baseURL, "/")
	url := fmt.Sprintf("%s/%s", strings.TrimRight(baseURL, "/"), strings.TrimLeft(o.baseModel.URLSuffix.Embedding, "/"))

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

	resp, err := o.baseModel.httpClient.Do(req)
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

	var parsed openaiEmbeddingResponse
	if err = json.Unmarshal(body, &parsed); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	var envelope map[string]json.RawMessage
	if json.Unmarshal(body, &envelope) != nil || envelope == nil {
		return nil, fmt.Errorf("invalid embeddings response")
	}
	if raw, ok := envelope["error"]; ok && string(raw) != "null" {
		return nil, fmt.Errorf("provider embeddings error")
	}
	var rawRows []map[string]json.RawMessage
	if json.Unmarshal(envelope["data"], &rawRows) != nil || len(rawRows) != len(texts) {
		return nil, fmt.Errorf("invalid embeddings count")
	}
	seen := make(map[int]bool)
	dimension := 0
	for i, row := range parsed.Data {
		if len(rawRows[i]["index"]) == 0 || string(rawRows[i]["index"]) == "null" || row.Index < 0 || row.Index >= len(texts) || seen[row.Index] || len(row.Embedding) == 0 {
			return nil, fmt.Errorf("invalid embedding index or vector")
		}
		var coordinates []json.RawMessage
		if json.Unmarshal(rawRows[i]["embedding"], &coordinates) != nil {
			return nil, fmt.Errorf("invalid embedding vector")
		}
		for _, raw := range coordinates {
			var value float64
			if string(raw) == "null" || json.Unmarshal(raw, &value) != nil {
				return nil, fmt.Errorf("invalid embedding coordinate")
			}
		}
		seen[row.Index] = true
		if dimension == 0 {
			dimension = len(row.Embedding)
		}
		if len(row.Embedding) != dimension {
			return nil, fmt.Errorf("inconsistent embedding dimension")
		}
		for _, value := range row.Embedding {
			if math.IsNaN(value) || math.IsInf(value, 0) {
				return nil, fmt.Errorf("nonfinite embedding")
			}
		}
	}

	var embeddings []EmbeddingData
	for _, dataElem := range parsed.Data {
		var embeddingData EmbeddingData
		embeddingData.Embedding = dataElem.Embedding
		embeddingData.Index = dataElem.Index
		embeddings = append(embeddings, embeddingData)
	}

	return embeddings, nil
}

// ListModels returns the list of model ids visible to the API key.
func (o *OpenAIModel) ListModels(apiConfig *APIConfig) ([]ListModelResponse, error) {
	if err := o.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	baseURL, err := o.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL = strings.TrimSuffix(baseURL, "/")
	url := fmt.Sprintf("%s/%s", strings.TrimRight(baseURL, "/"), strings.TrimLeft(o.baseModel.URLSuffix.Models, "/"))

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	// GET has no body, so Content-Type is not needed.
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := o.baseModel.httpClient.Do(req)
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
	if modelList.Models == nil {
		return nil, fmt.Errorf("invalid models list format")
	}

	return ParseListModel(modelList), nil
}

// Balance is not exposed by the OpenAI API, so this returns "no such method".
func (o *OpenAIModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("no such method")
}

// CheckConnection runs a lightweight ListModels call to verify the API key.
func (o *OpenAIModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := o.ListModels(apiConfig)
	if err != nil {
		return err
	}
	return nil
}

// Rerank calculates similarity scores between query and documents. OpenAI does
// not expose a rerank API, so this is left unimplemented.
func (o *OpenAIModel) Rerank(modelName *string, query string, documents []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return nil, fmt.Errorf("%s, Rerank not implemented", o.Name())
}

// TranscribeAudio transcribe audio
func (o *OpenAIModel) TranscribeAudio(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig) (*ASRResponse, error) {
	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, responseFormat, err := o.newOpenAIASRRequest(ctx, modelName, file, apiConfig, asrConfig, false)
	if err != nil {
		return nil, err
	}

	req.Header.Set("Accept", "application/json")

	resp, err := o.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(io.LimitReader(resp.Body, 20*1024*1024+1))
	if err == nil && len(respBody) > 20*1024*1024 {
		err = fmt.Errorf("response exceeds limit")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("provider HTTP error: status %d", resp.StatusCode)
	}

	return decodeOpenAIASRResponse(respBody, responseFormat)
}

func (o *OpenAIModel) TranscribeAudioWithSender(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	req, responseFormat, err := o.newOpenAIASRRequest(apiConfig.RequestContext(), modelName, file, apiConfig, asrConfig, true)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "text/event-stream")

	resp, err := o.baseModel.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {

		return fmt.Errorf("provider HTTP error: status %d", resp.StatusCode)
	}

	if !strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream") {
		respBody, err := io.ReadAll(io.LimitReader(resp.Body, 20*1024*1024+1))
		if err != nil {
			return fmt.Errorf("failed to read response body: %w", err)
		}
		response, err := decodeOpenAIASRResponse(respBody, responseFormat)
		if err != nil {
			return err
		}
		if response != nil && response.Text != "" {
			if err = sender(&response.Text, nil); err != nil {
				return err
			}
		}
		done := "[DONE]"
		return sender(&done, nil)
	}

	sentDelta := false
	if _, err = ParseSSEStream[struct {
		Type  string `json:"type"`
		Delta string `json:"delta"`
		Text  string `json:"text"`
	}](resp.Body, func(event struct {
		Type  string `json:"type"`
		Delta string `json:"delta"`
		Text  string `json:"text"`
	}) error {
		switch {
		case event.Delta != "":
			if err = sender(&event.Delta, nil); err != nil {
				return err
			}
			sentDelta = true
		case event.Type == "transcript.text.segment" && event.Text != "":
			if err = sender(&event.Text, nil); err != nil {
				return err
			}
			sentDelta = true
		case event.Type == "transcript.text.done" && !sentDelta && event.Text != "":
			if err = sender(&event.Text, nil); err != nil {
				return err
			}
		}
		return nil
	}); err != nil {
		return fmt.Errorf("error reading OpenAI ASR stream: %w", err)
	}

	done := "[DONE]"
	return sender(&done, nil)
}

func decodeOpenAIASRResponse(respBody []byte, responseFormat string) (*ASRResponse, error) {
	switch responseFormat {
	case "text", "srt", "vtt":
		return &ASRResponse{Text: string(respBody)}, nil
	}

	var result struct {
		Text string `json:"text"`
	}
	if err := json.Unmarshal(respBody, &result); err != nil {
		return nil, fmt.Errorf("invalid ASR response")
	}

	return &ASRResponse{Text: result.Text}, nil
}

// AudioSpeech convert text to audio
func (o *OpenAIModel) AudioSpeech(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig) (*TTSResponse, error) {
	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, _, err := o.newOpenAITTSRequest(ctx, modelName, audioContent, apiConfig, ttsConfig, false)
	if err != nil {
		return nil, err
	}

	resp, err := o.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 20*1024*1024+1))
	if err == nil && len(body) > 20*1024*1024 {
		err = fmt.Errorf("response exceeds limit")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("provider HTTP error: status %d", resp.StatusCode)
	}

	return &TTSResponse{Audio: body}, nil
}

func (o *OpenAIModel) AudioSpeechWithSender(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	req, streamFormat, err := o.newOpenAITTSRequest(apiConfig.RequestContext(), modelName, audioContent, apiConfig, ttsConfig, true)
	if err != nil {
		return err
	}
	if streamFormat == "sse" {
		req.Header.Set("Accept", "text/event-stream")
	}

	resp, err := o.baseModel.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {

		return fmt.Errorf("provider HTTP error: status %d", resp.StatusCode)
	}

	if streamFormat == "sse" || strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream") {
		return readOpenAITTSSSEStream(resp.Body, sender)
	}
	return readOpenAITTSRawStream(resp.Body, sender)
}

func (o *OpenAIModel) newOpenAIASRRequest(ctx context.Context, modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, stream bool) (*http.Request, string, error) {
	if err := o.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, "", err
	}
	if modelName == nil || *modelName == "" {
		return nil, "", fmt.Errorf("model name is required")
	}
	if file == nil || *file == "" {
		return nil, "", fmt.Errorf("file is missing")
	}
	if strings.TrimSpace(o.baseModel.URLSuffix.ASR) == "" {
		return nil, "", fmt.Errorf("openai ASR URL suffix is required")
	}

	baseURL, err := o.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, "", err
	}
	baseURL = strings.TrimSuffix(baseURL, "/")
	url := fmt.Sprintf("%s/%s", strings.TrimSuffix(baseURL, "/"), strings.TrimPrefix(o.baseModel.URLSuffix.ASR, "/"))

	var body bytes.Buffer
	writer := multipart.NewWriter(&body)

	audioFile, err := os.Open(*file)
	if err != nil {
		return nil, "", fmt.Errorf("failed to open audio file: %w", err)
	}
	defer audioFile.Close()

	part, err := writer.CreateFormFile("file", filepath.Base(*file))
	if err != nil {
		return nil, "", fmt.Errorf("failed to create multipart file: %w", err)
	}
	if _, err = io.Copy(part, audioFile); err != nil {
		return nil, "", fmt.Errorf("failed to copy audio data: %w", err)
	}
	if err = writer.WriteField("model", *modelName); err != nil {
		return nil, "", fmt.Errorf("failed to write model field: %w", err)
	}

	responseFormat := ""
	if asrConfig != nil && asrConfig.Params != nil {
		if value, ok := asrConfig.Params["response_format"].(string); ok {
			responseFormat = value
		}
		for key, value := range asrConfig.Params {
			if stream && key == "stream" {
				continue
			}
			if err = writeOpenAIMultipartField(writer, key, value); err != nil {
				return nil, "", err
			}
		}
	}
	if stream {
		if err = writer.WriteField("stream", "true"); err != nil {
			return nil, "", fmt.Errorf("failed to write stream field: %w", err)
		}
	}

	if err = writer.Close(); err != nil {
		return nil, "", fmt.Errorf("failed to close multipart writer: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, "POST", url, &body)
	if err != nil {
		return nil, "", fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))
	req.Header.Set("Content-Type", writer.FormDataContentType())
	return req, responseFormat, nil
}

func (o *OpenAIModel) newOpenAITTSRequest(ctx context.Context, modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, stream bool) (*http.Request, string, error) {
	if err := o.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, "", err
	}
	if modelName == nil || *modelName == "" {
		return nil, "", fmt.Errorf("model name is required")
	}
	if audioContent == nil || *audioContent == "" {
		return nil, "", fmt.Errorf("audio content is empty")
	}
	if strings.TrimSpace(o.baseModel.URLSuffix.TTS) == "" {
		return nil, "", fmt.Errorf("openai TTS URL suffix is required")
	}

	baseURL, err := o.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, "", err
	}
	baseURL = strings.TrimSuffix(baseURL, "/")
	url := fmt.Sprintf("%s/%s", strings.TrimSuffix(baseURL, "/"), strings.TrimPrefix(o.baseModel.URLSuffix.TTS, "/"))

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
	if stream {
		if _, ok := reqBody["stream_format"]; !ok {
			reqBody["stream_format"] = "audio"
		}
	}

	voice, ok := reqBody["voice"]
	if !ok || voice == nil {
		return nil, "", fmt.Errorf("voice is required")
	}
	voiceString, ok := voice.(string)
	if !ok || strings.TrimSpace(voiceString) == "" {
		return nil, "", fmt.Errorf("voice is required")
	}

	streamFormat := ""
	if value, ok := reqBody["stream_format"].(string); ok {
		streamFormat = value
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return nil, "", fmt.Errorf("failed to marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, "", fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))
	return req, streamFormat, nil
}

func readOpenAITTSSSEStream(body io.Reader, sender func(*string, *string) error) error {
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 64*1024), 8*1024*1024)

	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "data: ") {
			continue
		}

		dataStr := strings.TrimSpace(line[6:])
		if dataStr == "" || dataStr == "[DONE]" {
			continue
		}

		var event struct {
			Type  string `json:"type"`
			Audio string `json:"audio"`
		}
		if err := json.Unmarshal([]byte(dataStr), &event); err != nil {
			continue
		}

		if event.Type == "speech.audio.delta" && event.Audio != "" {
			audioBytes, err := base64.StdEncoding.DecodeString(event.Audio)
			if err == nil && len(audioBytes) > 0 {
				chunk := string(audioBytes)
				if errSend := sender(&chunk, nil); errSend != nil {
					return errSend
				}
			}
		}
		if event.Type == "speech.audio.done" {
			break
		}
	}

	if err := scanner.Err(); err != nil {
		return fmt.Errorf("error reading OpenAI TTS stream: %w", err)
	}
	return nil
}

func readOpenAITTSRawStream(body io.Reader, sender func(*string, *string) error) error {
	buf := make([]byte, 32*1024)
	for {
		n, err := body.Read(buf)
		if n > 0 {
			chunk := string(buf[:n])
			if errSend := sender(&chunk, nil); errSend != nil {
				return errSend
			}
		}
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return fmt.Errorf("error reading OpenAI TTS stream: %w", err)
		}
	}
}

func writeOpenAIMultipartField(writer *multipart.Writer, key string, value interface{}) error {
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

	if err := writer.WriteField(key, val); err != nil {
		return fmt.Errorf("failed to write field %s: %w", key, err)
	}
	return nil
}

// OCRFile OCR file
func (o *OpenAIModel) OCRFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, ocrConfig *OCRConfig) (*OCRFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", o.Name())
}

// ParseFile parse file
func (o *OpenAIModel) ParseFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*ParseFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", o.Name())
}

func (o *OpenAIModel) ListTasks(apiConfig *APIConfig) ([]ListTaskStatus, error) {
	return nil, fmt.Errorf("%s, no such method", o.Name())
}

func (o *OpenAIModel) ShowTask(taskID string, apiConfig *APIConfig) (*TaskResponse, error) {
	return nil, fmt.Errorf("%s, no such method", o.Name())
}
