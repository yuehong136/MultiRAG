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

package models

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

var xinferenceStreamIdleTimeout = 60 * time.Second

// XinferenceModel implements ModelDriver for Xinference chat models.
type XinferenceModel struct {
	baseModel BaseModel
}

type xinferenceChatChoice struct {
	Message struct {
		Content          string `json:"content"`
		ReasoningContent string `json:"reasoning_content"`
		Reasoning        string `json:"reasoning"`
		Thinking         string `json:"thinking"`
	} `json:"message"`
}

type xinferenceChatResponse struct {
	Choices []xinferenceChatChoice `json:"choices"`
}

type xinferenceModelListResponse struct {
	Data []DSModel `json:"data"`
}

// NewXinferenceModel creates a new Xinference model instance.
func NewXinferenceModel(baseURL map[string]string, urlSuffix URLSuffix) *XinferenceModel {
	if baseURL != nil {
		copied := make(map[string]string, len(baseURL))
		for region, value := range baseURL {
			copied[region] = value
		}
		baseURL = copied
	}
	return &XinferenceModel{
		baseModel: BaseModel{
			BaseURL:          baseURL,
			URLSuffix:        urlSuffix,
			AllowEmptyAPIKey: true,
			httpClient:       NewDriverHTTPClient(),
		},
	}
}

func (x *XinferenceModel) NewInstance(baseURL map[string]string) ModelDriver {
	return NewXinferenceModel(baseURL, x.baseModel.URLSuffix)
}

func (x *XinferenceModel) Name() string {
	return "xinference"
}

func normalizeXinferenceBaseURL(base string) string {
	trimmed := strings.TrimRight(strings.TrimSpace(base), "/")
	if trimmed == "" {
		return trimmed
	}
	if strings.HasSuffix(trimmed, "/v1") {
		return strings.TrimSuffix(trimmed, "/v1")
	}
	return trimmed
}

func xinferenceReasoningFromStrings(reasoningContent string, reasoning string, thinking string) string {
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

func xinferenceReasoningFromMap(value map[string]interface{}) string {
	for _, field := range []string{"reasoning_content", "reasoning", "thinking"} {
		if text, ok := value[field].(string); ok && text != "" {
			return text
		}
	}
	return ""
}

func buildXinferenceChatBody(modelName string, messages []Message, stream bool, chatModelConfig *ChatConfig) map[string]interface{} {
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
func (x *XinferenceModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	if err := x.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(messages) == 0 {
		return nil, fmt.Errorf("messages is empty")
	}

	baseURL, err := x.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL = normalizeXinferenceBaseURL(baseURL)
	url := fmt.Sprintf("%s/%s", baseURL, x.baseModel.URLSuffix.Chat)

	reqBody := buildXinferenceChatBody(modelName, messages, false, chatModelConfig)
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

	resp, err := x.baseModel.httpClient.Do(req)
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

	var result xinferenceChatResponse
	if err = json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}
	if len(result.Choices) == 0 {
		return nil, fmt.Errorf("no choices in response")
	}

	content := result.Choices[0].Message.Content
	reasonContent := xinferenceReasoningFromStrings(
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
func (x *XinferenceModel) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	idleTimeout := xinferenceStreamIdleTimeout

	if err := x.baseModel.APIConfigCheck(apiConfig); err != nil {
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

	baseURL, err := x.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return err
	}
	baseURL = normalizeXinferenceBaseURL(baseURL)
	url := fmt.Sprintf("%s/%s", baseURL, x.baseModel.URLSuffix.Chat)

	reqBody := buildXinferenceChatBody(modelName, messages, true, chatModelConfig)
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

	resp, err := x.baseModel.httpClient.Do(req)
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

	sawTerminal := false
	sseDone, parseErr := ParseSSEStream[map[string]interface{}](resp.Body, func(event map[string]interface{}) error {
		lastActiveMu.Lock()
		lastActive = time.Now()
		lastActiveMu.Unlock()

		choices, ok := event["choices"].([]interface{})
		if !ok || len(choices) == 0 {
			return nil
		}
		firstChoice, ok := choices[0].(map[string]interface{})
		if !ok {
			return nil
		}

		if delta, ok := firstChoice["delta"].(map[string]interface{}); ok {
			if reasoning := xinferenceReasoningFromMap(delta); reasoning != "" {
				if err := sender(nil, &reasoning); err != nil {
					return err
				}
			}
			if content, ok := delta["content"].(string); ok && content != "" {
				if err := sender(&content, nil); err != nil {
					return err
				}
			}
		}

		if finishReason, ok := firstChoice["finish_reason"].(string); ok && finishReason != "" {
			sawTerminal = true
		}
		return nil
	})
	if parseErr != nil {
		if ctx.Err() != nil {
			return fmt.Errorf("xinference: stream idle for more than %s, aborted", idleTimeout)
		}
		return fmt.Errorf("failed to scan response body: %w", parseErr)
	}
	if !sseDone && !sawTerminal {
		return fmt.Errorf("xinference: stream ended before [DONE] or finish_reason")
	}

	endOfStream := "[DONE]"
	return sender(&endOfStream, nil)
}

// Index is *int so a missing JSON field is distinguishable from index 0.
type xinferenceEmbeddingResponse struct {
	Data []struct {
		Index     *int      `json:"index"`
		Embedding []float64 `json:"embedding"`
	} `json:"data"`
}

// Embed POSTs the input texts to the tenant's Xinference
func (x *XinferenceModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	if len(texts) > 0 && (modelName == nil || strings.TrimSpace(*modelName) == "") {
		return nil, fmt.Errorf("model name is required")
	}

	if embeddingConfig != nil && embeddingConfig.Dimension < 0 {
		return nil, fmt.Errorf("embedding dimension must be nonnegative")
	}

	if err := x.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(texts) == 0 {
		return []EmbeddingData{}, nil
	}
	if modelName == nil || *modelName == "" {
		return nil, fmt.Errorf("model name is required")
	}

	baseURL, err := x.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL = normalizeXinferenceBaseURL(baseURL)
	if x.baseModel.URLSuffix.Embedding == "" {
		return nil, fmt.Errorf("xinference: no embedding URL suffix configured")
	}
	url := fmt.Sprintf("%s/%s", baseURL, x.baseModel.URLSuffix.Embedding)

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
	if auth := BearerAuth(apiConfig); auth != "" {
		req.Header.Set("Authorization", auth)
	}

	resp, err := x.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("Xinference embeddings API error: %s, body: %s", resp.Status, "[redacted]")
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

	var parsed xinferenceEmbeddingResponse
	if err = json.Unmarshal(body, &parsed); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	embeddings := make([]EmbeddingData, len(texts))
	seen := make([]bool, len(texts))
	for _, d := range parsed.Data {
		if d.Index == nil {
			return nil, fmt.Errorf("xinference: missing embedding index in response item")
		}
		idx := *d.Index
		if idx < 0 || idx >= len(texts) {
			return nil, fmt.Errorf("xinference: embedding index %d out of range for %d inputs", idx, len(texts))
		}
		if len(d.Embedding) == 0 {
			return nil, fmt.Errorf("xinference: missing embedding vector for response item at index %d", idx)
		}
		if seen[idx] {
			return nil, fmt.Errorf("xinference: duplicate embedding index %d", idx)
		}
		embeddings[idx] = EmbeddingData{Embedding: d.Embedding, Index: idx}
		seen[idx] = true
	}
	for i, ok := range seen {
		if !ok {
			return nil, fmt.Errorf("xinference: missing embedding for input at index %d", i)
		}
	}

	return embeddings, nil
}

type xinferenceRerankResult struct {
	Index          int     `json:"index"`
	RelevanceScore float64 `json:"relevance_score"`
}

type xinferenceRerankResponse struct {
	Results []xinferenceRerankResult `json:"results"`
}

// Rerank scores documents against the query using the Xinference
// /v1/rerank endpoint and returns one RerankResult per scored document
// in the API's ranking order. Caller may sort by Index to recover
// original input order. Xinference rerank models are launched with
// --model-type rerank and exposed under the OpenAI-compatible base URL.
func (x *XinferenceModel) Rerank(modelName *string, query string, documents []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
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

	if rerankConfig != nil && rerankConfig.TopN < 0 {
		return nil, fmt.Errorf("top_n must be nonnegative")
	}

	if err := x.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(documents) == 0 {
		return &RerankResponse{}, nil
	}
	if modelName == nil || *modelName == "" {
		return nil, fmt.Errorf("model name is required")
	}

	baseURL, err := x.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL = normalizeXinferenceBaseURL(baseURL)
	if x.baseModel.URLSuffix.Rerank == "" {
		return nil, fmt.Errorf("xinference: no rerank URL suffix configured")
	}
	url := fmt.Sprintf("%s/%s", baseURL, x.baseModel.URLSuffix.Rerank)

	topN := len(documents)
	if rerankConfig != nil && rerankConfig.TopN > 0 && rerankConfig.TopN < topN {
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

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	if auth := BearerAuth(apiConfig); auth != "" {
		req.Header.Set("Authorization", auth)
	}

	resp, err := x.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("Xinference rerank API error: %s, body: %s", resp.Status, "[redacted]")
	}

	var envelope map[string]json.RawMessage
	if json.Unmarshal(body, &envelope) != nil || envelope == nil {
		return nil, fmt.Errorf("invalid rerank response")
	}
	if value := envelope["error"]; value != nil && string(value) != "null" {
		return nil, fmt.Errorf("upstream rerank error")
	}
	var numeric []RerankResult
	expected := len(documents)
	if rerankConfig != nil && rerankConfig.TopN > 0 && rerankConfig.TopN < expected {
		expected = rerankConfig.TopN
	}
	if json.Unmarshal(envelope["results"], &numeric) != nil || len(numeric) != expected {
		return nil, fmt.Errorf("invalid rerank count or numeric values")
	}
	seenNumeric := make(map[int]bool, len(numeric))
	for _, row := range numeric {
		if row.Index < 0 || row.Index >= len(documents) || seenNumeric[row.Index] {
			return nil, fmt.Errorf("invalid rerank identity")
		}
		seenNumeric[row.Index] = true
	}

	var parsed xinferenceRerankResponse
	if err = json.Unmarshal(body, &parsed); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	rerankResponse := RerankResponse{Data: make([]RerankResult, 0, len(parsed.Results))}
	seen := make([]bool, len(documents))
	for _, item := range parsed.Results {
		if item.Index < 0 || item.Index >= len(documents) {
			return nil, fmt.Errorf("xinference: rerank index %d out of range for %d inputs", item.Index, len(documents))
		}
		if seen[item.Index] {
			return nil, fmt.Errorf("xinference: duplicate rerank index %d in response", item.Index)
		}
		rerankResponse.Data = append(rerankResponse.Data, RerankResult{
			Index:          item.Index,
			RelevanceScore: item.RelevanceScore,
		})
		seen[item.Index] = true
	}

	return &rerankResponse, nil
}

func (x *XinferenceModel) TranscribeAudio(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig) (*ASRResponse, error) {
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if err := x.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if file == nil || *file == "" {
		return nil, fmt.Errorf("file is missing")
	}

	resolvedBaseURL, err := x.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, x.baseModel.URLSuffix.ASR)

	var body bytes.Buffer
	writer := multipart.NewWriter(&body)

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

	if err = writer.WriteField("model", *modelName); err != nil {
		return nil, fmt.Errorf("failed to write model name: %w", err)
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

	if auth := BearerAuth(apiConfig); auth != "" {
		req.Header.Set("Authorization", auth)
	}
	req.Header.Set("Content-Type", writer.FormDataContentType())

	resp, err := x.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(io.LimitReader(resp.Body, (20<<20)+1))
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	if len(respBody) > 20<<20 {
		return nil, fmt.Errorf("media response exceeds limit")
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("FishAudio ASR error: %s - %s", resp.Status, "[redacted]")
	}

	var envelope map[string]json.RawMessage
	if json.Unmarshal(respBody, &envelope) != nil || envelope == nil || envelope["error"] != nil {
		return nil, fmt.Errorf("invalid transcription response")
	}
	if code := envelope["code"]; code != nil {
		var n float64
		if json.Unmarshal(code, &n) != nil || n != 0 {
			return nil, fmt.Errorf("transcription provider error")
		}
	}

	// result
	var result struct {
		Text *string `json:"text"`
	}

	if err := json.Unmarshal(respBody, &result); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response: %w", err)
	}

	if result.Text == nil {
		return nil, fmt.Errorf("ASR response contains no text")
	}

	return &ASRResponse{
		Text: *result.Text,
	}, nil
}

func (x *XinferenceModel) TranscribeAudioWithSender(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", x.Name())
}

func (x *XinferenceModel) AudioSpeech(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig) (*TTSResponse, error) {
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if err := x.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if audioContent == nil || *audioContent == "" {
		return nil, fmt.Errorf("text content is missing")
	}

	resolvedBaseURL, err := x.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, x.baseModel.URLSuffix.TTS)

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
		reqBody["format"] = ttsConfig.Format
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
	if auth := BearerAuth(apiConfig); auth != "" {
		req.Header.Set("Authorization", auth)
	}

	resp, err := x.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, (20<<20)+1))
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	if len(body) > 20<<20 {
		return nil, fmt.Errorf("media response exceeds limit")
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%s - %s", resp.Status, "[redacted]")
	}

	if len(body) == 0 || json.Valid(body) || strings.HasPrefix(strings.ToLower(resp.Header.Get("Content-Type")), "application/json") {
		return nil, fmt.Errorf("speech response contains no audio")
	}
	return &TTSResponse{Audio: body}, nil
}

func (x *XinferenceModel) AudioSpeechWithSender(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", x.Name())
}

func (x *XinferenceModel) OCRFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, ocrConfig *OCRConfig) (*OCRFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", x.Name())
}

func (x *XinferenceModel) ParseFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*ParseFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", x.Name())
}

// ListModels returns the model IDs exposed by Xinference's OpenAI-compatible
// /v1/models endpoint.
func (x *XinferenceModel) ListModels(apiConfig *APIConfig) ([]ListModelResponse, error) {
	if err := x.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	baseURL, err := x.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL = normalizeXinferenceBaseURL(baseURL)
	url := fmt.Sprintf("%s/%s", baseURL, x.baseModel.URLSuffix.Models)

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

	resp, err := x.baseModel.httpClient.Do(req)
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

	var result xinferenceModelListResponse
	if err = json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	return ParseListModel(ModelList{Models: result.Data}), nil
}

func (x *XinferenceModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("%s, no such method", x.Name())
}

func (x *XinferenceModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := x.ListModels(apiConfig)
	return err
}

func (x *XinferenceModel) ListTasks(apiConfig *APIConfig) ([]ListTaskStatus, error) {
	return nil, fmt.Errorf("%s, no such method", x.Name())
}

func (x *XinferenceModel) ShowTask(taskID string, apiConfig *APIConfig) (*TaskResponse, error) {
	return nil, fmt.Errorf("%s, no such method", x.Name())
}
