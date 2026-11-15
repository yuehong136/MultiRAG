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
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"unicode/utf8"
)

type DeepInfraModel struct {
	baseModel BaseModel
}

func NewDeepInfraModel(baseURL map[string]string, urlSuffix URLSuffix) *DeepInfraModel {
	if baseURL != nil {
		copied := make(map[string]string, len(baseURL))
		for region, value := range baseURL {
			copied[region] = value
		}
		baseURL = copied
	}
	return &DeepInfraModel{
		baseModel: BaseModel{
			BaseURL:    baseURL,
			URLSuffix:  urlSuffix,
			httpClient: NewDriverHTTPClient(),
		},
	}
}

func (d *DeepInfraModel) NewInstance(baseURL map[string]string) ModelDriver {
	return NewDeepInfraModel(baseURL, d.baseModel.URLSuffix)
}

func (d *DeepInfraModel) Name() string {
	return "deepinfra"
}

func (d *DeepInfraModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	if err := d.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(messages) == 0 {
		return nil, fmt.Errorf("messages is empty")
	}

	resolvedBaseURL, err := d.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", strings.TrimRight(resolvedBaseURL, "/"), d.baseModel.URLSuffix.Chat)

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

		if chatModelConfig.Effort != nil {
			reqBody["reasoning_effort"] = *chatModelConfig.Effort
		}

		if chatModelConfig.Thinking != nil && *chatModelConfig.Thinking {
			reasoningMap := map[string]interface{}{
				"enabled": true,
			}
			if chatModelConfig.Effort != nil {
				reasoningMap["effort"] = *chatModelConfig.Effort
			}
			reqBody["reasoning"] = reasoningMap
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
	if len(body) > 20*1024*1024 {
		return nil, fmt.Errorf("provider response exceeds20MiB")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	// Parse result
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
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response body: %w", err)
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
	}

	chatResponse := &ChatResponse{
		Answer: &content,
	}
	if reasonContent != "" {
		chatResponse.ReasonContent = &reasonContent
	}

	return chatResponse, nil
}

func (d *DeepInfraModel) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, modelConfig *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("sender is required")
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
	url := fmt.Sprintf("%s/%s", strings.TrimRight(resolvedBaseURL, "/"), d.baseModel.URLSuffix.Chat)

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
			reqBody["reasoning_effort"] = *modelConfig.Effort
		}

		if modelConfig.Thinking != nil && *modelConfig.Thinking {
			reasoningMap := map[string]interface{}{
				"enabled": true,
			}
			if modelConfig.Effort != nil {
				reasoningMap["effort"] = *modelConfig.Effort
			}
			reqBody["reasoning"] = reasoningMap
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

		choices, ok := event["choices"].([]interface{})
		if !ok || len(choices) == 0 {
			return nil
		}

		firstChoice, ok := choices[0].(map[string]interface{})
		if !ok {
			return nil
		}

		delta, ok := firstChoice["delta"].(map[string]interface{})
		if !ok {
			return nil
		}

		reasoningContent, ok := delta["reasoning_content"].(string)
		if ok && reasoningContent != "" {
			if err := sender(nil, &reasoningContent); err != nil {
				return err
			}
			if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
				return parentErr
			}

		}

		content, ok := delta["content"].(string)
		if ok && content != "" {
			if err := sender(&content, nil); err != nil {
				return err
			}
			if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
				return parentErr
			}

		}

		finishReason, ok := firstChoice["finish_reason"].(string)
		if ok && finishReason != "" {
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
		return fmt.Errorf("deepinfra: stream ended before [DONE] or finish_reason")
	}

	// Send [DONE] marker for OpenAI compatibility
	if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
		return parentErr
	}
	if !responseHasContent || (!done && !responseHasTerminal) {
		return fmt.Errorf("stream ended before successful content completion")
	}

	endOfStream := "[DONE]"
	return sender(&endOfStream, nil)
}

func (d *DeepInfraModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	if len(texts) > 0 && (modelName == nil || strings.TrimSpace(*modelName) == "") {
		return nil, fmt.Errorf("model name is required")
	}

	if embeddingConfig != nil && (embeddingConfig.Dimension < 0 || embeddingConfig.Dimension > 0 && embeddingConfig.Dimension < 32) {
		return nil, fmt.Errorf("embedding dimension must be nonnegative")
	}

	if err := d.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(texts) == 0 {
		return []EmbeddingData{}, fmt.Errorf("texts is empty")
	}

	resolvedBaseURL, err := d.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", strings.TrimRight(resolvedBaseURL, "/"), d.baseModel.URLSuffix.Embedding)

	reqBody := map[string]interface{}{
		"model": *modelName,
		"input": texts,
	}

	if embeddingConfig != nil && embeddingConfig.Dimension >= 32 {
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

	resp, err := d.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 20*1024*1024+1))
	if len(body) > 20*1024*1024 {
		return nil, fmt.Errorf("provider response exceeds20MiB")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("DeepInfra embedding API error: status %d, body: %s", resp.StatusCode, "[redacted]")
	}

	var parsed struct {
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
	if err = json.Unmarshal(body, &parsed); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response: %w", err)
	}

	var embeddings []EmbeddingData
	for _, data := range parsed.Data {
		embeddings = append(embeddings, EmbeddingData{
			Embedding: data.Embedding,
			Index:     data.Index,
		})
	}

	return embeddings, nil
}

// deepinfraRerankResponse is the JSON body returned by DeepInfra reranker models.
type deepinfraRerankResponse struct {
	Scores []float64 `json:"scores"`
}

// Rerank scores documents against a query using DeepInfra's inference endpoint.
func (d *DeepInfraModel) Rerank(modelName *string, query string, documents []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
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

	if err := d.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(documents) == 0 {
		return &RerankResponse{}, nil
	}
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if strings.ContainsAny(*modelName, "?%#\\") {
		return nil, fmt.Errorf("invalid rerank model path")
	}
	for _, part := range strings.Split(*modelName, "/") {
		if part == "." || part == ".." || part == "" {
			return nil, fmt.Errorf("invalid rerank model path")
		}
	}
	resolvedBaseURL, err := d.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL := resolvedBaseURL

	// Reranker model ids may contain slashes (e.g. Qwen/Qwen3-Reranker-4B).
	url := fmt.Sprintf("%s/%s/%s", strings.TrimSuffix(baseURL, "/"), d.baseModel.URLSuffix.Rerank, *modelName)

	reqBody := map[string]interface{}{
		"query":     query,
		"documents": documents,
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
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := d.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 20*1024*1024+1))
	if len(body) > 20*1024*1024 {
		return nil, fmt.Errorf("provider response exceeds20MiB")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("DeepInfra rerank API error: status %d, body: %s", resp.StatusCode, "[redacted]")
	}

	var parsed deepinfraRerankResponse

	var responseEnvelope map[string]json.RawMessage
	if json.Unmarshal(body, &responseEnvelope) != nil || responseEnvelope == nil || responseEnvelope["error"] != nil {
		return nil, fmt.Errorf("invalid rerank response")
	}

	var numericEnvelope struct {
		Scores []*float64 `json:"scores"`
	}
	if json.Unmarshal(body, &numericEnvelope) != nil || len(numericEnvelope.Scores) != len(documents) {
		return nil, fmt.Errorf("invalid rerank scores")
	}
	for _, score := range numericEnvelope.Scores {
		if score == nil {
			return nil, fmt.Errorf("null rerank score")
		}
	}
	if err = json.Unmarshal(body, &parsed); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}
	if len(parsed.Scores) != len(documents) {
		return nil, fmt.Errorf("deepinfra: expected %d scores, got %d", len(documents), len(parsed.Scores))
	}

	results := make([]RerankResult, len(parsed.Scores))
	for i, score := range parsed.Scores {
		results[i] = RerankResult{
			Index:          i,
			RelevanceScore: score,
		}
	}

	topN := len(results)
	if rerankConfig != nil && rerankConfig.TopN > 0 && rerankConfig.TopN < topN {
		topN = rerankConfig.TopN
		slices.SortFunc(results, func(a, b RerankResult) int {
			if a.RelevanceScore > b.RelevanceScore {
				return -1
			}
			if a.RelevanceScore < b.RelevanceScore {
				return 1
			}
			return 0
		})
		results = results[:topN]
	}

	return &RerankResponse{Data: results}, nil
}

func (d *DeepInfraModel) TranscribeAudio(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig) (*ASRResponse, error) {
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if err := d.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if file == nil || *file == "" {
		return nil, fmt.Errorf("file is missing")
	}

	if modelName == nil || *modelName == "" {
		return nil, fmt.Errorf("model name is missing")
	}

	resolvedBaseURL, err := d.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", strings.TrimRight(resolvedBaseURL, "/"), d.baseModel.URLSuffix.ASR)

	var body bytes.Buffer
	writer := multipart.NewWriter(&body)

	if err := writer.WriteField("model", *modelName); err != nil {
		return nil, fmt.Errorf("failed to write model field: %w", err)
	}

	// Open File
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

	// get config
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
			case float64:
				val = strconv.FormatFloat(v, 'f', -1, 64)
			case float32:
				val = strconv.FormatFloat(float64(v), 'f', -1, 32)
			default:
				val = fmt.Sprintf("%v", v)
			}

			if err := writer.WriteField(key, val); err != nil {
				return nil, fmt.Errorf("failed to write field %s: %w", key, err)
			}
		}
	}

	if err = writer.Close(); err != nil {
		return nil, fmt.Errorf("failed to close multipart writer: %w", err)
	}

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), longOpCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "POST", url, &body)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))
	req.Header.Set("Content-Type", writer.FormDataContentType())
	req.Header.Set("Accept", "application/json")

	resp, err := d.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(io.LimitReader(resp.Body, 20*1024*1024+1))
	if len(respBody) > 20*1024*1024 {
		return nil, fmt.Errorf("provider response exceeds20MiB")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("DeepInfra ASR error: %s - %s", resp.Status, "[redacted]")
	}

	// Parse result
	var result struct {
		Text *string `json:"text"`
	}

	if err := json.Unmarshal(respBody, &result); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response: %w", err)
	}

	var envelope map[string]json.RawMessage
	if json.Unmarshal(respBody, &envelope) != nil || result.Text == nil || (envelope["error"] != nil && string(envelope["error"]) != "null") || (envelope["code"] != nil && string(envelope["code"]) != "0" && string(envelope["code"]) != "null") {
		return nil, fmt.Errorf("invalid transcription response")
	}
	return &ASRResponse{
		Text: *result.Text,
	}, nil
}

func (d *DeepInfraModel) TranscribeAudioWithSender(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s no such method", d.Name())
}

func (d *DeepInfraModel) AudioSpeech(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig) (*TTSResponse, error) {
	if err := d.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if audioContent == nil || *audioContent == "" {
		return nil, fmt.Errorf("text content is missing")
	}

	reqBody := map[string]interface{}{
		"text": *audioContent,
	}
	voiceID := ""

	if ttsConfig != nil && ttsConfig.Params != nil {
		if v, ok := ttsConfig.Params["voice_id"].(string); ok && v != "" {
			voiceID = v
		} else if v, ok := ttsConfig.Params["voice"].(string); ok && v != "" {
			voiceID = v
		}

		for key, value := range ttsConfig.Params {
			if key != "voice_id" && key != "voice" {
				reqBody[key] = value
			}
		}
	}

	if voiceID == "" {
		return nil, fmt.Errorf("voice_id is missing (must be provided in params or model name)")
	}

	if strings.ContainsAny(voiceID, "/\\?#%") || voiceID == "." || voiceID == ".." {
		return nil, fmt.Errorf("invalid voice path")
	}
	// URL: https://api.deepinfra.com/v1/text-to-speech/{voice_id}
	resolvedBaseURL, err := d.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s/%s", resolvedBaseURL, d.baseModel.URLSuffix.TTS, voiceID)

	if ttsConfig != nil && ttsConfig.Format != "" {
		reqBody["output_format"] = ttsConfig.Format
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

	resp, err := d.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 20*1024*1024+1))
	if len(body) > 20*1024*1024 {
		return nil, fmt.Errorf("provider response exceeds20MiB")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("DeepInfra TTS error: status %d - %s", resp.StatusCode, "[redacted]")
	}

	contentType := strings.ToLower(resp.Header.Get("Content-Type"))
	if len(body) == 0 || strings.Contains(contentType, "json") || strings.Contains(contentType, "text/") || json.Valid(body) {
		return nil, fmt.Errorf("speech response contains no audio")
	}
	if ttsConfig != nil && ttsConfig.Format == "wav" && strings.Contains(contentType, "audio/mpeg") {
		return nil, fmt.Errorf("speech format mismatch")
	}
	return &TTSResponse{Audio: body}, nil
}

func (d *DeepInfraModel) AudioSpeechWithSender(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	if err := d.baseModel.APIConfigCheck(apiConfig); err != nil {
		return err
	}

	if audioContent == nil || *audioContent == "" {
		return fmt.Errorf("text content is missing")
	}

	voiceID := ""

	reqBody := map[string]interface{}{
		"text": *audioContent,
	}

	if ttsConfig != nil && ttsConfig.Params != nil {
		if v, ok := ttsConfig.Params["voice_id"].(string); ok && v != "" {
			voiceID = v
		} else if v, ok := ttsConfig.Params["voice"].(string); ok && v != "" {
			voiceID = v
		}

		for key, value := range ttsConfig.Params {
			if key != "voice_id" && key != "voice" {
				reqBody[key] = value
			}
		}
	}

	if voiceID == "" {
		return fmt.Errorf("voice_id is missing")
	}

	if strings.ContainsAny(voiceID, "/\\?#%") || voiceID == "." || voiceID == ".." {
		return fmt.Errorf("invalid voice path")
	}
	// URL: https://api.deepinfra.com/v1/text-to-speech/{voice_id}/stream
	resolvedBaseURL, err := d.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return err
	}
	url := fmt.Sprintf("%s/%s/%s/stream", resolvedBaseURL, d.baseModel.URLSuffix.TTS, voiceID)

	if ttsConfig != nil && ttsConfig.Format != "" {
		reqBody["output_format"] = ttsConfig.Format
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
		return fmt.Errorf("DeepInfra TTS Stream error: status %d - %s", resp.StatusCode, "[redacted]")
	}

	contentType := strings.ToLower(resp.Header.Get("Content-Type"))
	if strings.Contains(contentType, "json") || strings.Contains(contentType, "text/") {
		return fmt.Errorf("speech response contains no audio")
	}
	if ttsConfig != nil && ttsConfig.Format == "wav" && strings.Contains(contentType, "audio/mpeg") {
		return fmt.Errorf("speech format mismatch")
	}
	received := 0
	buffer := make([]byte, 4096)
	for {
		n, err := resp.Body.Read(buffer)

		if n > 0 {
			received += n
			if received > 20*1024*1024 {
				return fmt.Errorf("speech stream exceeds20MiB")
			}
			if !utf8.Valid(buffer[:n]) {
				return fmt.Errorf("binary speech cannot be represented by the text stream sender")
			}
			chunkStr := string(buffer[:n])
			if sendErr := sender(&chunkStr, nil); sendErr != nil {
				return sendErr
			}
			if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
				return parentErr
			}
		}

		if err == io.EOF {
			break
		}
		if err != nil {
			return fmt.Errorf("error reading stream: %w", err)
		}
	}

	if received == 0 {
		return fmt.Errorf("speech stream contains no audio")
	}
	if err := apiConfig.RequestContext().Err(); err != nil {
		return err
	}
	endOfStream := "[DONE]"
	if err = sender(&endOfStream, nil); err != nil {
		return err
	}
	if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
		return parentErr
	}

	return nil
}

func (d *DeepInfraModel) OCRFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, ocrConfig *OCRConfig) (*OCRFileResponse, error) {
	return nil, fmt.Errorf("%s no such method", d.Name())
}

func (d *DeepInfraModel) ParseFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*ParseFileResponse, error) {
	return nil, fmt.Errorf("%s no such method", d.Name())
}

func (d *DeepInfraModel) ListModels(apiConfig *APIConfig) ([]ListModelResponse, error) {

	resolvedBaseURL, err := d.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", strings.TrimRight(resolvedBaseURL, "/"), d.baseModel.URLSuffix.Models)

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")

	resp, err := d.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 20*1024*1024+1))
	if len(body) > 20*1024*1024 {
		return nil, fmt.Errorf("provider response exceeds20MiB")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("failed to read response: %s", "[redacted]")
	}

	if strings.TrimSpace(string(body)) == "null" {
		return nil, fmt.Errorf("models response is null")
	}
	// Parse response
	var result []struct {
		ModelName string `json:"model_name"`
	}
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response: %w", err)
	}

	models := make([]DSModel, 0, len(result))
	for _, model := range result {
		if model.ModelName != "" {
			models = append(models, DSModel{
				ID:      model.ModelName,
				OwnedBy: d.Name(),
			})
		}
	}

	return ParseListModel(ModelList{Models: models}), nil
}

func (d *DeepInfraModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	if err := d.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	baseURL, err := d.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", strings.TrimRight(baseURL, "/"), d.baseModel.URLSuffix.Balance)

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
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
	if len(body) > 20*1024*1024 {
		return nil, fmt.Errorf("provider response exceeds20MiB")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("failed to read response: %s", "[redacted]")
	}

	var result struct {
		Balance *float64 `json:"stripe_balance"`
	}

	if err := json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response: %w", err)
	}

	var envelope map[string]json.RawMessage
	if json.Unmarshal(body, &envelope) != nil || result.Balance == nil || (envelope["error"] != nil && string(envelope["error"]) != "null") {
		return nil, fmt.Errorf("invalid balance response")
	}
	return map[string]interface{}{
		"balance":  *result.Balance,
		"currence": "USD",
	}, nil
}

func (d *DeepInfraModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := d.ListModels(apiConfig)
	return err
}

func (d *DeepInfraModel) ListTasks(apiConfig *APIConfig) ([]ListTaskStatus, error) {
	return nil, fmt.Errorf("%s no such method", d.Name())
}

func (d *DeepInfraModel) ShowTask(taskID string, apiConfig *APIConfig) (*TaskResponse, error) {
	return nil, fmt.Errorf("%s no such method", d.Name())
}
