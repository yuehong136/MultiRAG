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
	"net/http"
	"strings"
)

// OllamaModel implements ModelDriver for Ollama AI
type OllamaModel struct {
	baseModel BaseModel
}

// contentToText extracts a plain-text string from a Message.Content value.
// Content may be a raw string or an OpenAI-style multimodal array
// ([]interface{} where each element is {"type": "text", "text": "..."}).
// All text parts are retained in order; empty string on unsupported or invalid input.
func contentToText(content interface{}) string {
	if text, ok := content.(string); ok {
		return text
	}
	var parts []interface{}
	switch values := content.(type) {
	case []interface{}:
		parts = values
	case []map[string]interface{}:
		for _, value := range values {
			parts = append(parts, value)
		}
	}
	texts := []string{}
	for _, item := range parts {
		part, ok := item.(map[string]interface{})
		if !ok {
			return ""
		}
		if part["type"] == "text" {
			text, ok := part["text"].(string)
			if !ok {
				return ""
			}
			texts = append(texts, text)
		}
	}
	return strings.Join(texts, "\n")
}

// NewOllamaModel creates a new Ollama AI model instance
func NewOllamaModel(baseURL map[string]string, urlSuffix URLSuffix) *OllamaModel {
	if baseURL != nil {
		copied := make(map[string]string, len(baseURL))
		for region, value := range baseURL {
			copied[region] = value
		}
		baseURL = copied
	}
	return &OllamaModel{
		baseModel: BaseModel{
			BaseURL:          baseURL,
			URLSuffix:        urlSuffix,
			AllowEmptyAPIKey: true,
			httpClient:       NewDriverHTTPClient(),
		},
	}
}

func (o *OllamaModel) NewInstance(baseURL map[string]string) ModelDriver {
	return NewOllamaModel(baseURL, o.baseModel.URLSuffix)
}

func (o *OllamaModel) Name() string {
	return "ollama"
}

func (o *OllamaModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	if chatModelConfig == nil {
		chatModelConfig = &ChatConfig{}
	}
	if strings.TrimSpace(modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if len(messages) == 0 {
		return nil, fmt.Errorf("message is nil")
	}

	resolvedBaseURL, err := o.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, o.baseModel.URLSuffix.Chat)

	// For qwen/glm models, use async chat endpoint
	modelType := strings.Split(modelName, "_")[0]
	if modelType == "qwen" || modelType == "glm" {
		url = fmt.Sprintf("%s/%s", resolvedBaseURL, o.baseModel.URLSuffix.AsyncChat)
	}

	// Convert messages to API format
	apiMessages := make([]map[string]interface{}, len(messages))
	for i, msg := range messages {
		text := contentToText(msg.Content)
		if text == "" {
			return nil, fmt.Errorf("invalid message content")
		}
		apiMessages[i] = map[string]interface{}{"role": msg.Role, "content": text}
		var parts []interface{}
		switch values := msg.Content.(type) {
		case []interface{}:
			parts = values
		case []map[string]interface{}:
			for _, value := range values {
				parts = append(parts, value)
			}
		}
		images := []string{}
		for _, value := range parts {
			part, ok := value.(map[string]interface{})
			if !ok {
				return nil, fmt.Errorf("invalid message part")
			}
			switch part["type"] {
			case "text":
			case "image_url":
				image, ok := part["image_url"].(map[string]interface{})
				if !ok {
					return nil, fmt.Errorf("invalid image part")
				}
				raw, ok := image["url"].(string)
				if !ok {
					return nil, fmt.Errorf("invalid image data")
				}
				header, data, ok := strings.Cut(raw, ",")
				if !ok || !strings.HasPrefix(header, "data:image/") || !strings.HasSuffix(header, ";base64") {
					return nil, fmt.Errorf("Ollama requires inline base64 images")
				}
				decoded, err := base64.StdEncoding.Strict().DecodeString(data)
				if err != nil || len(decoded) == 0 {
					return nil, fmt.Errorf("invalid image data")
				}
				images = append(images, data)
			default:
				return nil, fmt.Errorf("unsupported message part")
			}
		}
		if len(images) > 0 {
			apiMessages[i]["images"] = images
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

		if chatModelConfig.Effort != nil && *chatModelConfig.Effort != "" {
			if strings.HasPrefix(strings.ToLower(modelName), "gpt-oss") {
				reqBody["think"] = *chatModelConfig.Effort
			}
		} else if chatModelConfig.Thinking != nil {
			if *chatModelConfig.Thinking {
				reqBody["think"] = true
			}
		}
	}

	options := map[string]interface{}{}
	for _, field := range []string{"temperature", "top_p", "stop"} {
		if value, ok := reqBody[field]; ok {
			options[field] = value
			delete(reqBody, field)
		}
	}
	if value, ok := reqBody["max_tokens"]; ok {
		options["num_predict"] = value
		delete(reqBody, "max_tokens")
	}
	if len(options) > 0 {
		reqBody["options"] = options
	}
	if chatModelConfig.Thinking != nil && (chatModelConfig.Effort == nil || *chatModelConfig.Effort == "") {
		reqBody["think"] = *chatModelConfig.Thinking
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
	var result map[string]interface{}
	if err = json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	if result == nil || result["error"] != nil || result["done"] != true {
		return nil, fmt.Errorf("incomplete Ollama chat response")
	}
	message, ok := result["message"].(map[string]interface{})
	if !ok {
		return nil, fmt.Errorf("failed to parse response: message not found")
	}

	content, ok := message["content"].(string)
	if !ok {
		return nil, fmt.Errorf("failed to parse response: content not found")
	}

	reasonContent, _ := message["thinking"].(string)

	chatResponse := &ChatResponse{
		Answer:        &content,
		ReasonContent: &reasonContent,
	}

	return chatResponse, nil
}

func (o *OllamaModel) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, modelConfig *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	if modelConfig == nil {
		modelConfig = &ChatConfig{}
	}
	if strings.TrimSpace(modelName) == "" {
		return fmt.Errorf("model name is required")
	}

	if len(messages) == 0 {
		return fmt.Errorf("messages is empty")
	}

	resolvedBaseURL, err := o.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, o.baseModel.URLSuffix.Chat)
	modelType := strings.Split(modelName, "-")[0]
	if modelType == "qwen" || modelType == "glm" {
		url = fmt.Sprintf("%s/%s", resolvedBaseURL, o.baseModel.URLSuffix.AsyncChat)
	}

	// Convert messages to API format (supporting multimodal content)
	apiMessages := make([]map[string]interface{}, len(messages))
	for i, msg := range messages {
		text := contentToText(msg.Content)
		if text == "" {
			return fmt.Errorf("invalid message content")
		}
		apiMessages[i] = map[string]interface{}{"role": msg.Role, "content": text}
		var parts []interface{}
		switch values := msg.Content.(type) {
		case []interface{}:
			parts = values
		case []map[string]interface{}:
			for _, value := range values {
				parts = append(parts, value)
			}
		}
		images := []string{}
		for _, value := range parts {
			part, ok := value.(map[string]interface{})
			if !ok {
				return fmt.Errorf("invalid message part")
			}
			switch part["type"] {
			case "text":
			case "image_url":
				image, ok := part["image_url"].(map[string]interface{})
				if !ok {
					return fmt.Errorf("invalid image part")
				}
				raw, ok := image["url"].(string)
				if !ok {
					return fmt.Errorf("invalid image data")
				}
				header, data, ok := strings.Cut(raw, ",")
				if !ok || !strings.HasPrefix(header, "data:image/") || !strings.HasSuffix(header, ";base64") {
					return fmt.Errorf("Ollama requires inline base64 images")
				}
				decoded, err := base64.StdEncoding.Strict().DecodeString(data)
				if err != nil || len(decoded) == 0 {
					return fmt.Errorf("invalid image data")
				}
				images = append(images, data)
			default:
				return fmt.Errorf("unsupported message part")
			}
		}
		if len(images) > 0 {
			apiMessages[i]["images"] = images
		}
	}

	// Build request body with streaming enabled
	reqBody := map[string]interface{}{
		"model":    modelName,
		"messages": apiMessages,
		"stream":   true,
	}

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

	if modelConfig.Effort != nil && *modelConfig.Effort != "" {
		if strings.HasPrefix(strings.ToLower(modelName), "gpt-oss") {
			reqBody["think"] = *modelConfig.Effort
		}
	} else if modelConfig.Thinking != nil {
		if *modelConfig.Thinking {
			reqBody["think"] = true
		}
	}

	options := map[string]interface{}{}
	for _, field := range []string{"temperature", "top_p", "stop"} {
		if value, ok := reqBody[field]; ok {
			options[field] = value
			delete(reqBody, field)
		}
	}
	if value, ok := reqBody["max_tokens"]; ok {
		options["num_predict"] = value
		delete(reqBody, "max_tokens")
	}
	if len(options) > 0 {
		reqBody["options"] = options
	}
	if modelConfig.Thinking != nil && (modelConfig.Effort == nil || *modelConfig.Effort == "") {
		reqBody["think"] = *modelConfig.Thinking
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

	resp, err := o.baseModel.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 20*1024*1024))
		return fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	sawContent, sawTerminal := false, false
	// SSE parsing: read line by line
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())

		// ignore the blank
		if line == "" {
			continue
		}

		// Parse the JSON event
		var event map[string]interface{}
		if err = json.Unmarshal([]byte(line), &event); err != nil {
			return fmt.Errorf("invalid Ollama stream event")
		}
		if event == nil || event["error"] != nil {
			return fmt.Errorf("Ollama stream error")
		}

		if messageMap, ok := event["message"].(map[string]interface{}); ok {
			if reasoningContent, exists := messageMap["thinking"].(string); exists && reasoningContent != "" {
				sawContent = true
				if err := sender(nil, &reasoningContent); err != nil {
					return err
				}
			}
			if err := ctx.Err(); err != nil {
				return err
			}
			if content, exists := messageMap["content"].(string); exists && content != "" {
				sawContent = true
				if err := sender(&content, nil); err != nil {
					return err
				}
			}
		}

		if err := ctx.Err(); err != nil {
			return err
		}
		if done, ok := event["done"].(bool); ok && done {
			sawTerminal = true
			break
		}
	}

	if err := ctx.Err(); err != nil {
		return err
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	if !sawContent || !sawTerminal {
		return fmt.Errorf("incomplete Ollama stream")
	}
	// Send [DONE] marker for OpenAI compatibility with RAGFlow frontend
	endOfStream := "[DONE]"
	if err := sender(&endOfStream, nil); err != nil {
		return err
	}

	return scanner.Err()
}

func (o *OllamaModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	if len(texts) > 0 && (modelName == nil || strings.TrimSpace(*modelName) == "") {
		return nil, fmt.Errorf("model name is required")
	}

	if len(texts) > 0 && o.baseModel.URLSuffix.Embedding == "" {
		return nil, fmt.Errorf("embedding URL suffix is required")
	}

	if embeddingConfig != nil && embeddingConfig.Dimension < 0 {
		return nil, fmt.Errorf("embedding dimension must be nonnegative")
	}

	if err := o.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(texts) == 0 {
		return []EmbeddingData{}, nil
	}

	if modelName == nil || *modelName == "" {
		return nil, fmt.Errorf("model name is required")
	}

	resolvedBaseURL, err := o.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL := resolvedBaseURL
	if baseURL == "" {
		baseURL = resolvedBaseURL
	}
	if baseURL == "" {
		return nil, fmt.Errorf("missing base URL: please configure the local access address for Ollama (e.g., http://127.0.0.1:11434/v1)")
	}

	url := fmt.Sprintf("%s/%s", strings.TrimSuffix(baseURL, "/"), o.baseModel.URLSuffix.Embedding)

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

	resp, err := o.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("Ollama embeddings API error: %s, body: %s", resp.Status, "[redacted]")
	}

	var envelope map[string]json.RawMessage
	if json.Unmarshal(body, &envelope) != nil || envelope == nil || envelope["error"] != nil {
		return nil, fmt.Errorf("invalid embedding response")
	}
	var numeric [][]*float64
	if json.Unmarshal(envelope["embeddings"], &numeric) != nil || len(numeric) != len(texts) {
		return nil, fmt.Errorf("invalid embedding count")
	}
	dimension := 0
	for _, row := range numeric {
		if len(row) == 0 {
			return nil, fmt.Errorf("empty embedding")
		}
		if dimension == 0 {
			dimension = len(row)
		} else if dimension != len(row) {
			return nil, fmt.Errorf("inconsistent embedding dimension")
		}
		if embeddingConfig != nil && embeddingConfig.Dimension > 0 && len(row) != embeddingConfig.Dimension {
			return nil, fmt.Errorf("unexpected embedding dimension")
		}
		for _, value := range row {
			if value == nil {
				return nil, fmt.Errorf("null embedding value")
			}
		}
	}

	var embedResp struct {
		Model      string      `json:"model"`
		Embeddings [][]float64 `json:"embeddings"`
	}

	if err = json.Unmarshal(body, &embedResp); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response: %w", err)
	}

	if len(embedResp.Embeddings) == 0 {
		return nil, fmt.Errorf("no embeddings returned")
	}

	embeddings := make([]EmbeddingData, 0, len(embedResp.Embeddings))

	for i, emb := range embedResp.Embeddings {
		if len(emb) == 0 {
			return nil, fmt.Errorf("empty embedding at index %d", i)
		}

		embeddings = append(embeddings, EmbeddingData{
			Embedding: emb,
			Index:     i,
		})
	}

	return embeddings, nil
}

func (o *OllamaModel) Rerank(modelName *string, query string, documents []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return nil, fmt.Errorf("no such method")
}

// TranscribeAudio transcribe audio
func (o *OllamaModel) TranscribeAudio(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig) (*ASRResponse, error) {
	return nil, fmt.Errorf("%s, no such method", o.Name())
}

func (o *OllamaModel) TranscribeAudioWithSender(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", o.Name())
}

// AudioSpeech convert text to audio
func (o *OllamaModel) AudioSpeech(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig) (*TTSResponse, error) {
	return nil, fmt.Errorf("%s, no such method", o.Name())
}

func (o *OllamaModel) AudioSpeechWithSender(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", o.Name())
}

// OCRFile OCR file
func (o *OllamaModel) OCRFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, ocrConfig *OCRConfig) (*OCRFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", o.Name())
}

// ParseFile parse file
func (o *OllamaModel) ParseFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*ParseFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", o.Name())
}

func (o *OllamaModel) ListModels(apiConfig *APIConfig) ([]ListModelResponse, error) {

	resolvedBaseURL, err := o.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL := resolvedBaseURL
	if baseURL == "" {
		baseURL = resolvedBaseURL
	}
	if baseURL == "" {
		return nil, fmt.Errorf("missing base URL: please configure the local access address for Ollama (e.g., http://127.0.0.1:11434/v1)")
	}

	url := fmt.Sprintf("%s/%s", baseURL, o.baseModel.URLSuffix.Models)
	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")

	resp, err := o.baseModel.httpClient.Do(req)
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

	rawModels, ok := result["models"].([]interface{})
	if !ok || result["error"] != nil {
		return nil, fmt.Errorf("invalid Ollama inventory")
	}
	models := make([]ListModelResponse, 0, len(rawModels))
	for _, raw := range rawModels {
		model, ok := raw.(map[string]interface{})
		if !ok {
			return nil, fmt.Errorf("invalid model record")
		}
		name, ok := model["name"].(string)
		if !ok || strings.TrimSpace(name) == "" {
			return nil, fmt.Errorf("invalid model name")
		}
		models = append(models, ListModelResponse{Name: name})
	}
	return models, nil
}

func (o *OllamaModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("no such method")
}

// CheckConnection verifies that the configured Ollama base URL is reachable
func (o *OllamaModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := o.ListModels(apiConfig)
	return err
}

func (o *OllamaModel) ListTasks(apiConfig *APIConfig) ([]ListTaskStatus, error) {
	return nil, fmt.Errorf("%s, no such method", o.Name())
}

func (o *OllamaModel) ShowTask(taskID string, apiConfig *APIConfig) (*TaskResponse, error) {
	return nil, fmt.Errorf("%s, no such method", o.Name())
}
