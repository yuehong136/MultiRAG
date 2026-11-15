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
	"mime/multipart"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// CometAPIModel implements ModelDriver for CometAPI AI.
type CometAPIModel struct {
	baseModel BaseModel
}

// NewCometAPIModel creates a new CometAPI model instance.
func NewCometAPIModel(baseURL map[string]string, urlSuffix URLSuffix) *CometAPIModel {
	if baseURL != nil {
		copied := make(map[string]string, len(baseURL))
		for region, value := range baseURL {
			copied[region] = value
		}
		baseURL = copied
	}
	return &CometAPIModel{
		baseModel: BaseModel{
			BaseURL:    baseURL,
			URLSuffix:  urlSuffix,
			httpClient: NewDriverHTTPClient(),
		},
	}
}

func (c *CometAPIModel) NewInstance(baseURL map[string]string) ModelDriver {
	return NewCometAPIModel(baseURL, c.baseModel.URLSuffix)
}

func (c *CometAPIModel) Name() string {
	return "cometapi"
}

func validateCometAPIModelName(modelName string) error {
	if strings.TrimSpace(modelName) == "" {
		return fmt.Errorf("model name is required")
	}
	return nil
}

func cometapiRegion(apiConfig *APIConfig) string {
	if apiConfig != nil && apiConfig.Region != nil && *apiConfig.Region != "" {
		return *apiConfig.Region
	}
	return "default"
}

func (c *CometAPIModel) endpointURL(region, suffix string) (string, error) {
	baseURL, err := c.baseModel.GetBaseURL(&APIConfig{Region: &region})
	if err != nil {
		return "", err
	}
	baseURL = strings.TrimSuffix(baseURL, "/")
	return fmt.Sprintf("%s/%s", baseURL, strings.TrimLeft(suffix, "/")), nil
}

func (c *CometAPIModel) balanceURL(apiKey string) string {
	rawURL := strings.TrimSpace(c.baseModel.URLSuffix.Balance)
	if !strings.HasPrefix(rawURL, "http://") && !strings.HasPrefix(rawURL, "https://") {
		rawURL = fmt.Sprintf("https://query.cometapi.com/%s", strings.TrimLeft(rawURL, "/"))
	}
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return rawURL
	}
	query := parsed.Query()
	query.Set("key", apiKey)
	parsed.RawQuery = query.Encode()
	return parsed.String()
}

type cometapiChatRequest struct {
	Model       string               `json:"model"`
	Messages    []cometapiAPIMessage `json:"messages"`
	Stream      bool                 `json:"stream"`
	MaxTokens   *int                 `json:"max_tokens,omitempty"`
	Temperature *float64             `json:"temperature,omitempty"`
	TopP        *float64             `json:"top_p,omitempty"`
	Stop        *[]string            `json:"stop,omitempty"`
}

type cometapiAPIMessage struct {
	Role    string      `json:"role"`
	Content interface{} `json:"content"`
}

func buildCometAPIChatRequest(modelName string, messages []Message, stream bool, chatModelConfig *ChatConfig) cometapiChatRequest {
	apiMessages := make([]cometapiAPIMessage, len(messages))
	for i, msg := range messages {
		apiMessages[i] = cometapiAPIMessage{
			Role:    msg.Role,
			Content: msg.Content,
		}
	}

	reqBody := cometapiChatRequest{
		Model:    modelName,
		Messages: apiMessages,
		Stream:   stream,
	}
	if chatModelConfig != nil {
		reqBody.MaxTokens = chatModelConfig.MaxTokens
		reqBody.Temperature = chatModelConfig.Temperature
		reqBody.TopP = chatModelConfig.TopP
		reqBody.Stop = chatModelConfig.Stop
	}
	return reqBody
}

func newCometAPIJSONRequest(ctx context.Context, method string, endpoint string, payload interface{}, apiKey string) (*http.Request, error) {
	jsonData, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, method, endpoint, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if apiKey != "" {
		req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", apiKey))
	}
	return req, nil
}

type cometapiHTTPResponse struct {
	StatusCode int
	Status     string
	Body       []byte
}

func (c *CometAPIModel) doCometAPIRequest(req *http.Request) (*cometapiHTTPResponse, error) {
	resp, err := c.baseModel.httpClient.Do(req)
	if err != nil {
		if req.Context().Err() != nil {
			return nil, req.Context().Err()
		}
		return nil, fmt.Errorf("CometAPI request failed")
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 20*1024*1024+1))
	if len(body) > 20*1024*1024 {
		return nil, fmt.Errorf("provider response exceeds20MiB")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	return &cometapiHTTPResponse{
		StatusCode: resp.StatusCode,
		Status:     resp.Status,
		Body:       body,
	}, nil
}

type cometapiChatResponsePayload struct {
	Choices []cometapiChatChoice `json:"choices"`
}

type cometapiChatChoice struct {
	Message      cometapiChatMessage `json:"message"`
	Delta        cometapiChatDelta   `json:"delta"`
	FinishReason string              `json:"finish_reason"`
}

type cometapiChatMessage struct {
	Content          *string `json:"content"`
	ReasoningContent string  `json:"reasoning_content"`
}

type cometapiChatDelta struct {
	Content          string `json:"content"`
	ReasoningContent string `json:"reasoning_content"`
}

func parseCometAPIChatResponse(body []byte) (*ChatResponse, error) {
	var envelope map[string]json.RawMessage
	if json.Unmarshal(body, &envelope) != nil || envelope == nil || (len(envelope["error"]) > 0 && string(envelope["error"]) != "null") {
		return nil, fmt.Errorf("CometAPI invalid response")
	}
	var parsed cometapiChatResponsePayload
	if err := json.Unmarshal(body, &parsed); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}
	if len(parsed.Choices) == 0 {
		return nil, fmt.Errorf("no choices in response")
	}
	if parsed.Choices[0].Message.Content == nil {
		return nil, fmt.Errorf("invalid content format")
	}

	content := *parsed.Choices[0].Message.Content
	if strings.TrimSpace(content) == "" {
		return nil, fmt.Errorf("CometAPI empty response")
	}
	reasonContent := strings.TrimLeft(parsed.Choices[0].Message.ReasoningContent, "\n")
	return &ChatResponse{
		Answer:        &content,
		ReasonContent: &reasonContent,
	}, nil
}

func parseCometAPIStreamEvent(data string) (content string, reasonContent string, terminal bool, ok bool) {
	var event cometapiChatResponsePayload
	if err := json.Unmarshal([]byte(data), &event); err != nil {
		return "", "", false, false
	}
	if len(event.Choices) == 0 {
		return "", "", false, false
	}
	choice := event.Choices[0]
	return choice.Delta.Content, choice.Delta.ReasoningContent, choice.FinishReason != "", true
}

type cometapiModelCatalogResponse struct {
	Data []cometapiModelCatalogItem `json:"data"`
}

type cometapiModelCatalogItem struct {
	ID string `json:"id"`
}

// ChatWithMessages sends multiple messages with roles and returns the response.
func (c *CometAPIModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	if err := c.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}
	apiKey := *apiConfig.ApiKey
	if err := validateCometAPIModelName(modelName); err != nil {
		return nil, err
	}

	if len(messages) == 0 {
		return nil, fmt.Errorf("messages is empty")
	}

	url, err := c.endpointURL(cometapiRegion(apiConfig), c.baseModel.URLSuffix.Chat)
	if err != nil {
		return nil, err
	}

	reqBody := buildCometAPIChatRequest(modelName, messages, false, chatModelConfig)

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := newCometAPIJSONRequest(ctx, "POST", url, reqBody, apiKey)
	if err != nil {
		return nil, err
	}
	resp, err := c.doCometAPIRequest(req)
	if err != nil {
		return nil, err
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, "[redacted]")
	}
	return parseCometAPIChatResponse(resp.Body)
}

// ChatStreamlyWithSender sends messages and streams the response
func (c *CometAPIModel) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	if err := c.baseModel.APIConfigCheck(apiConfig); err != nil {
		return err
	}

	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	if err := validateCometAPIModelName(modelName); err != nil {
		return err
	}

	if len(messages) == 0 {
		return fmt.Errorf("messages is empty")
	}

	apiKey := *apiConfig.ApiKey

	url, err := c.endpointURL(cometapiRegion(apiConfig), c.baseModel.URLSuffix.Chat)
	if err != nil {
		return err
	}

	if chatModelConfig != nil {
		// Refuse to run if the caller explicitly asked for stream=false.
		// The body of this method only knows how to read SSE, so a
		// non-SSE JSON response would be parsed as if it were a stream
		// and produce no chunks. Better to fail clearly.
		if chatModelConfig.Stream != nil && !*chatModelConfig.Stream {
			return fmt.Errorf("stream must be true in ChatStreamlyWithSender")
		}
	}
	reqBody := buildCometAPIChatRequest(modelName, messages, true, chatModelConfig)

	req, err := newCometAPIJSONRequest(apiConfig.RequestContext(), "POST", url, reqBody, apiKey)
	if err != nil {
		return err
	}
	resp, err := c.baseModel.httpClient.Do(req)
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

		rawJSON, _ := json.Marshal(rawEvent)
		var event cometapiChatResponsePayload
		if err := json.Unmarshal(rawJSON, &event); err != nil {
			return fmt.Errorf("invalid stream event: %w", err)
		}

		if len(event.Choices) == 0 {
			return nil
		}
		choice := event.Choices[0]
		reasoningContent := choice.Delta.ReasoningContent
		content := choice.Delta.Content

		if reasoningContent != "" {
			if err := sender(nil, &reasoningContent); err != nil {
				return err
			}
			if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
				return parentErr
			}

		}

		if content != "" {
			if err := sender(&content, nil); err != nil {
				return err
			}
			if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
				return parentErr
			}

		}

		if choice.FinishReason != "" {
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
		return fmt.Errorf("cometapi: stream ended before [DONE] or finish_reason")
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

type cometapiEmbeddingData struct {
	Embedding []float64 `json:"embedding"`
	Object    string    `json:"object"`
	Index     int       `json:"index"`
}

type cometapiEmbeddingResponse struct {
	Data   []cometapiEmbeddingData `json:"data"`
	Model  string                  `json:"model"`
	Object string                  `json:"object"`
}

type cometapiEmbeddingRequest struct {
	Model      string   `json:"model"`
	Input      []string `json:"input"`
	Dimensions int      `json:"dimensions,omitempty"`
}

// Embed turns a list of texts into embedding vectors
func (c *CometAPIModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	if len(texts) > 0 && (modelName == nil || strings.TrimSpace(*modelName) == "") {
		return nil, fmt.Errorf("model name is required")
	}

	if err := c.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(texts) == 0 {
		return []EmbeddingData{}, nil
	}

	apiKey := *apiConfig.ApiKey

	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}

	url, err := c.endpointURL(cometapiRegion(apiConfig), c.baseModel.URLSuffix.Embedding)
	if err != nil {
		return nil, err
	}

	reqBody := cometapiEmbeddingRequest{
		Model: *modelName,
		Input: texts,
	}
	if embeddingConfig != nil && embeddingConfig.Dimension > 0 {
		reqBody.Dimensions = embeddingConfig.Dimension
	}

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := newCometAPIJSONRequest(ctx, "POST", url, reqBody, apiKey)
	if err != nil {
		return nil, err
	}

	resp, err := c.doCometAPIRequest(req)
	if err != nil {
		return nil, err
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("CometAPI embeddings API error: %s, body: %s", resp.Status, "[redacted]")
	}

	var parsed cometapiEmbeddingResponse
	var numericEnvelope map[string]json.RawMessage
	if json.Unmarshal(resp.Body, &numericEnvelope) != nil || numericEnvelope == nil || numericEnvelope["error"] != nil {
		return nil, fmt.Errorf("invalid embedding response")
	}
	var numericRows []EmbeddingData
	if json.Unmarshal(numericEnvelope["data"], &numericRows) != nil || len(numericRows) != len(texts) {
		return nil, fmt.Errorf("invalid embedding count or values")
	}
	seenNumeric := map[int]bool{}
	dimension := 0
	for _, row := range numericRows {
		if row.Index < 0 || row.Index >= len(texts) || seenNumeric[row.Index] {
			return nil, fmt.Errorf("invalid embedding identity")
		}
		seenNumeric[row.Index] = true
		if dimension == 0 {
			dimension = len(row.Embedding)
		} else if dimension != len(row.Embedding) {
			return nil, fmt.Errorf("inconsistent embedding dimension")
		}
		if embeddingConfig != nil && embeddingConfig.Dimension > 0 && len(row.Embedding) != embeddingConfig.Dimension {
			return nil, fmt.Errorf("unexpected embedding dimension")
		}
	}
	if err = json.Unmarshal(resp.Body, &parsed); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	embeddings := make([]EmbeddingData, len(texts))
	filled := make([]bool, len(texts))
	for _, item := range parsed.Data {
		if item.Index < 0 || item.Index >= len(texts) {
			return nil, fmt.Errorf("cometapi: response index %d out of range for %d inputs", item.Index, len(texts))
		}
		if filled[item.Index] {
			return nil, fmt.Errorf("cometapi: duplicate embedding index %d in response", item.Index)
		}
		embeddings[item.Index] = EmbeddingData{
			Embedding: item.Embedding,
			Index:     item.Index,
		}
		filled[item.Index] = true
	}
	for i, ok := range filled {
		if !ok {
			return nil, fmt.Errorf("cometapi: missing embedding for input index %d", i)
		}
	}

	return embeddings, nil
}

// ListModels returns the public CometAPI model catalog.
func (c *CometAPIModel) ListModels(apiConfig *APIConfig) ([]ListModelResponse, error) {
	url, err := c.endpointURL(cometapiRegion(apiConfig), c.baseModel.URLSuffix.Models)
	if err != nil {
		return nil, err
	}

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	resp, err := c.doCometAPIRequest(req)
	if err != nil {
		return nil, err
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	// Parse response
	var modelList ModelList
	var inventoryEnvelope map[string]json.RawMessage
	if json.Unmarshal(resp.Body, &inventoryEnvelope) != nil || inventoryEnvelope == nil || inventoryEnvelope["data"] == nil || string(inventoryEnvelope["data"]) == "null" {
		return nil, fmt.Errorf("models response missing data")
	}
	if value := inventoryEnvelope["error"]; len(value) > 0 && string(value) != "null" {
		return nil, fmt.Errorf("model discovery business error")
	}
	if code := inventoryEnvelope["code"]; len(code) > 0 && string(code) != "0" && string(code) != "\"0\"" && string(code) != "null" {
		return nil, fmt.Errorf("model discovery business error")
	}
	if err = json.Unmarshal(resp.Body, &modelList); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}
	return ParseListModel(modelList), nil
}

// Balance queries CometAPI's quota service.
func (c *CometAPIModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	if err := c.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}
	if strings.TrimSpace(c.baseModel.URLSuffix.Balance) == "" {
		return nil, fmt.Errorf("balance URL is required")
	}

	quotaURL, err := url.Parse(c.balanceURL(*apiConfig.ApiKey))
	if err != nil || quotaURL.Host == "" || quotaURL.User != nil || quotaURL.Fragment != "" || (quotaURL.Scheme != "http" && quotaURL.Scheme != "https") {
		return nil, fmt.Errorf("invalid quota URL")
	}
	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", c.balanceURL(*apiConfig.ApiKey), nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	resp, err := c.doCometAPIRequest(req)
	if err != nil {
		return nil, err
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("CometAPI quota API error: %s, body: %s", resp.Status, "[redacted]")
	}

	var result map[string]interface{}
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	if result == nil {
		return nil, fmt.Errorf("quota response is null")
	}
	quota, ok := result["total_quota"].(float64)
	if !ok || math.IsNaN(quota) || math.IsInf(quota, 0) || quota < 0 {
		return nil, fmt.Errorf("invalid quota response")
	}
	for _, key := range []string{"total_used_quota", "request_count"} {
		if raw, exists := result[key]; exists {
			value, ok := raw.(float64)
			if !ok || math.IsNaN(value) || math.IsInf(value, 0) || value < 0 || key == "request_count" && math.Trunc(value) != value {
				return nil, fmt.Errorf("invalid quota field")
			}
		}
	}
	if result["error"] != nil || result["success"] == false {
		return nil, fmt.Errorf("quota request failed")
	}
	return result, nil
}

// CheckConnection runs a quota query to verify the API key.
func (c *CometAPIModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := c.Balance(apiConfig)
	if err != nil {
		return err
	}
	return nil
}

// Rerank calculates similarity scores between query and documents.
func (c *CometAPIModel) Rerank(modelName *string, query string, documents []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return nil, fmt.Errorf("no such method")
}

// TranscribeAudio transcribe audio
func (c *CometAPIModel) TranscribeAudio(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig) (*ASRResponse, error) {
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if err := c.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if file == nil || *file == "" {
		return nil, fmt.Errorf("file is missing")
	}

	resolvedBaseURL, err := c.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, c.baseModel.URLSuffix.ASR)

	// multipart body
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)

	// open audio file
	audioFile, err := os.Open(*file)
	if err != nil {
		return nil, fmt.Errorf("failed to open audio file: %w", err)
	}
	defer audioFile.Close()

	// create multipart file field
	part, err := writer.CreateFormFile("file", filepath.Base(*file))
	if err != nil {
		return nil, fmt.Errorf("failed to create multipart file: %w", err)
	}

	// copy file content
	if _, err = io.Copy(part, audioFile); err != nil {
		return nil, fmt.Errorf("failed to copy audio data: %w", err)
	}

	// model field
	if err := writer.WriteField("model", *modelName); err != nil {
		return nil, fmt.Errorf("failed to write model field: %w", err)
	}

	// extra params
	if asrConfig != nil && asrConfig.Params != nil {
		for key, value := range asrConfig.Params {
			if key == "model" || key == "file" || key == "stream" {
				return nil, fmt.Errorf("reserved ASR parameter %s", key)
			}

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
	req, err := http.NewRequestWithContext(apiConfig.RequestContext(), "POST", url, &body)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))
	req.Header.Set("Content-Type", writer.FormDataContentType())
	req.Header.Set("Accept", "application/json")

	// send request
	resp, err := c.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(io.LimitReader(resp.Body, 20*1024*1024+1))
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("SiliconFlow ASR error: %s - %s", resp.Status, "[redacted]")
	}

	// SiliconFlow response
	var result struct {
		Text *string `json:"text"`
	}

	if err = json.Unmarshal(respBody, &result); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response: %w, body=%s", err, "[redacted]")
	}

	if result.Text == nil {
		return nil, fmt.Errorf("ASR response missing text")
	}
	return &ASRResponse{Text: *result.Text}, nil
}

func (c *CometAPIModel) TranscribeAudioWithSender(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", c.Name())
}

// AudioSpeech synthesizes speech audio from text.
func (c *CometAPIModel) AudioSpeech(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig) (*TTSResponse, error) {
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if err := c.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if audioContent == nil || *audioContent == "" {
		return nil, fmt.Errorf("audio content is empty")
	}

	resolvedBaseURL, err := c.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, c.baseModel.URLSuffix.TTS)

	reqBody := map[string]interface{}{
		"model": *modelName,
		"input": *audioContent,
	}

	if ttsConfig != nil && ttsConfig.Params != nil {
		for key, value := range ttsConfig.Params {
			if key == "model" || key == "input" || key == "stream" {
				return nil, fmt.Errorf("reserved speech parameter %s", key)
			}
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

	req, err := http.NewRequestWithContext(apiConfig.RequestContext(), "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := c.baseModel.httpClient.Do(req)
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
		return nil, fmt.Errorf("%s - %s", resp.Status, "[redacted]")
	}

	if len(bytes.TrimSpace(body)) == 0 || json.Valid(body) || strings.Contains(strings.ToLower(resp.Header.Get("Content-Type")), "json") {
		return nil, fmt.Errorf("speech response contains no audio")
	}
	return &TTSResponse{Audio: body}, nil
}

func (c *CometAPIModel) AudioSpeechWithSender(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", c.Name())
}

// OCRFile OCR file
func (c *CometAPIModel) OCRFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, ocrConfig *OCRConfig) (*OCRFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", c.Name())
}

func (c *CometAPIModel) ParseFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*ParseFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", c.Name())
}

func (c *CometAPIModel) ListTasks(apiConfig *APIConfig) ([]ListTaskStatus, error) {
	return nil, fmt.Errorf("%s, no such method", c.Name())
}

func (c *CometAPIModel) ShowTask(taskID string, apiConfig *APIConfig) (*TaskResponse, error) {
	return nil, fmt.Errorf("%s, no such method", c.Name())
}
