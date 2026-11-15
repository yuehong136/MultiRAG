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
	"multirag/internal/common"
	"net/http"
	neturl "net/url"
	"sort"
	"strings"
	"time"
)

// GiteeModel implements ModelDriver for Gitee
type GiteeModel struct {
	baseModel BaseModel
}

// NewGiteeModel creates a new Gitee model instance
func NewGiteeModel(baseURL map[string]string, urlSuffix URLSuffix) *GiteeModel {
	if baseURL != nil {
		copied := make(map[string]string, len(baseURL))
		for region, value := range baseURL {
			copied[region] = value
		}
		baseURL = copied
	}
	return &GiteeModel{
		baseModel: BaseModel{
			BaseURL:    baseURL,
			URLSuffix:  urlSuffix,
			httpClient: NewDriverHTTPClient(),
		},
	}
}

func (g *GiteeModel) NewInstance(baseURL map[string]string) ModelDriver {
	return NewGiteeModel(baseURL, g.baseModel.URLSuffix)
}

func (g *GiteeModel) Name() string {
	return "gitee"
}

// ChatWithMessages sends multiple messages with roles and returns response
func (g *GiteeModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	if err := g.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(messages) == 0 {
		return nil, fmt.Errorf("messages is empty")
	}

	resolvedBaseURL, err := g.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, g.baseModel.URLSuffix.Chat)

	// Convert messages to the format expected by API
	apiMessages := make([]map[string]interface{}, len(messages))
	for i, msg := range messages {
		apiMessages[i] = map[string]interface{}{
			"role":    msg.Role,
			"content": msg.Content,
		}
	}
	common.Info(fmt.Sprintf("GiteeAPI messages: %+v", apiMessages))

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

	common.Info(fmt.Sprintf("GiteeAPI request body: %s", string(jsonData)))

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := g.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, (20<<20)+1))
	if len(body) > 20<<20 {
		return nil, fmt.Errorf("provider response exceeds limit")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	// Parse response
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

	// Handle thinking/reasoning if enabled
	var reasonContent string
	if chatModelConfig != nil && chatModelConfig.Thinking != nil && *chatModelConfig.Thinking {
		// Try to get reasoning_content directly first
		if rc, ok := messageMap["reasoning_content"].(string); ok && rc != "" {
			reasonContent = rc
			if reasonContent[0] == '\n' {
				reasonContent = reasonContent[1:]
			}
		} else {
			// Fall back to parsing <think> tags from content
			reasoning, answer := GetThinkingAndAnswer(chatModelConfig.ModelClass, &content)
			if reasoning != nil {
				reasonContent = *reasoning
				content = *answer
			}
		}
	}

	chatResponse := &ChatResponse{
		Answer:        &content,
		ReasonContent: &reasonContent,
	}

	return chatResponse, nil
}

// ChatStreamlyWithSender sends messages and streams response via sender function (best performance, no channel)
func (g *GiteeModel) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	if chatModelConfig == nil {
		chatModelConfig = &ChatConfig{}
	}

	if err := g.baseModel.APIConfigCheck(apiConfig); err != nil {
		return err
	}

	if len(messages) == 0 {
		return fmt.Errorf("messages is empty")
	}

	resolvedBaseURL, err := g.baseModel.GetBaseURL(apiConfig)
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
			reqBody["thinking"] = map[string]interface{}{
				"type": "enabled",
			}
		} else {
			reqBody["thinking"] = map[string]interface{}{
				"type": "disabled",
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

	resp, err := g.baseModel.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	reserveText := ""
	thinkingPhase := false
	answerPhase := false
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

		if reason, ok := delta["reasoning_content"].(string); ok && reason != "" {
			if err := sender(nil, &reason); err != nil {
				return err
			}
			if err := apiConfig.RequestContext().Err(); err != nil {
				return err
			}
		}
		if content, ok := delta["content"].(string); ok && content != "" {
			reserveText += content
			for reserveText != "" {
				if !thinkingPhase && !answerPhase {
					if strings.HasPrefix("<think>", reserveText) && len(reserveText) < len("<think>") {
						break
					}
					if strings.HasPrefix(reserveText, "<think>") {
						reserveText = strings.TrimPrefix(reserveText, "<think>")
						thinkingPhase = true
					} else {
						answerPhase = true
					}
				}
				if thinkingPhase {
					end := strings.Index(reserveText, "</think>")
					if end >= 0 {
						reason := reserveText[:end]
						reserveText = reserveText[end+len("</think>"):]
						if reason != "" {
							if err := sender(nil, &reason); err != nil {
								return err
							}
							if err := apiConfig.RequestContext().Err(); err != nil {
								return err
							}
						}
						thinkingPhase = false
						answerPhase = true
						continue
					}
					keep := 0
					for n := 1; n < len("</think>") && n <= len(reserveText); n++ {
						if strings.HasSuffix(reserveText, "</think>"[:n]) {
							keep = n
						}
					}
					reason := reserveText[:len(reserveText)-keep]
					reserveText = reserveText[len(reserveText)-keep:]
					if reason != "" {
						if err := sender(nil, &reason); err != nil {
							return err
						}
						if err := apiConfig.RequestContext().Err(); err != nil {
							return err
						}
					}
					break
				}
				if answerPhase {
					answer := reserveText
					reserveText = ""
					if err := sender(&answer, nil); err != nil {
						return err
					}
					if err := apiConfig.RequestContext().Err(); err != nil {
						return err
					}
				}
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
		return fmt.Errorf("gitee: stream ended before [DONE] or finish_reason")
	}

	if reserveText != "" {
		if err = sender(&reserveText, nil); err != nil {
			return err
		}
	}

	// Send [DONE] marker for OpenAI compatibility
	if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
		return parentErr
	}
	if !responseHasContent || (!done && !responseHasTerminal) {
		return fmt.Errorf("stream ended before successful content completion")
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

type giteeEmbeddingResponse struct {
	Object string               `json:"object"`
	Data   []giteeEmbeddingData `json:"data"`
	Model  string               `json:"model"`
	Usage  giteeUsage           `json:"usage"`
}

type giteeEmbeddingData struct {
	Object    string    `json:"object"`
	Embedding []float64 `json:"embedding"`
	Index     int       `json:"index"`
}

type giteeUsage struct {
	PromptTokens int `json:"prompt_tokens"`
	TotalTokens  int `json:"total_tokens"`
}

// Embed embeds a list of texts into embeddings
func (g *GiteeModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	if len(texts) > 0 && (modelName == nil || strings.TrimSpace(*modelName) == "") {
		return nil, fmt.Errorf("model name is required")
	}

	if err := g.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(texts) == 0 {
		return []EmbeddingData{}, nil
	}

	if modelName == nil || *modelName == "" {
		return nil, fmt.Errorf("model name is required")
	}

	resolvedBaseURL, err := g.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL := resolvedBaseURL

	url := fmt.Sprintf("%s/%s", strings.TrimSuffix(baseURL, "/"), g.baseModel.URLSuffix.Embedding)

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

	resp, err := g.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, (20<<20)+1))
	if len(body) > 20<<20 {
		return nil, fmt.Errorf("provider response exceeds limit")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("Gitee embeddings API error: %s, body: %s", resp.Status, "[redacted]")
	}

	var envelope map[string]json.RawMessage
	if json.Unmarshal(body, &envelope) != nil || (envelope["error"] != nil && string(envelope["error"]) != "null") {
		return nil, fmt.Errorf("invalid embedding response")
	}
	var embeddings []EmbeddingData
	if json.Unmarshal(envelope["data"], &embeddings) != nil || len(embeddings) != len(texts) {
		return nil, fmt.Errorf("embedding response cardinality mismatch")
	}
	seen := map[int]bool{}
	dimension := 0
	for _, row := range embeddings {
		if row.Index < 0 || row.Index >= len(texts) || seen[row.Index] || len(row.Embedding) == 0 {
			return nil, fmt.Errorf("invalid embedding result")
		}
		seen[row.Index] = true
		if dimension == 0 {
			dimension = len(row.Embedding)
		}
		if dimension != len(row.Embedding) || (embeddingConfig != nil && embeddingConfig.Dimension > 0 && dimension != embeddingConfig.Dimension) {
			return nil, fmt.Errorf("embedding dimension mismatch")
		}
	}
	return embeddings, nil
}

type giteeRerankRequest struct {
	Model           string   `json:"model"`
	Query           string   `json:"query"`
	Documents       []string `json:"documents"`
	TopN            int      `json:"top_n"`
	ReturnDocuments bool     `json:"return_documents"`
}

// Rerank calculates similarity scores between query and documents
func (g *GiteeModel) Rerank(modelName *string, query string, documents []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
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

	if err := g.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(documents) == 0 {
		return &RerankResponse{}, nil
	}

	if modelName == nil || *modelName == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if strings.TrimSpace(g.baseModel.URLSuffix.Rerank) == "" {
		return nil, fmt.Errorf("rerank endpoint is required")
	}

	resolvedBaseURL, err := g.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL := resolvedBaseURL

	url := fmt.Sprintf("%s/%s", strings.TrimSuffix(baseURL, "/"), g.baseModel.URLSuffix.Rerank)

	var topN = rerankConfig.TopN
	if rerankConfig.TopN == 0 {
		topN = len(documents)
	}

	reqBody := giteeRerankRequest{
		Model:           *modelName,
		Query:           query,
		Documents:       documents,
		TopN:            topN,
		ReturnDocuments: false,
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

	resp, err := g.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, (20<<20)+1))
	if len(body) > 20<<20 {
		return nil, fmt.Errorf("provider response exceeds limit")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("gitee rerank API error: %s, body: %s", resp.Status, "[redacted]")
	}

	var rerankResponse RerankResponse
	if err = json.Unmarshal(body, &rerankResponse); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	var envelope map[string]json.RawMessage
	expected := topN
	if expected > len(documents) {
		expected = len(documents)
	}
	if json.Unmarshal(body, &envelope) != nil || (envelope["error"] != nil && string(envelope["error"]) != "null") || len(rerankResponse.Data) != expected {
		return nil, fmt.Errorf("invalid rerank response")
	}
	seen := map[int]bool{}
	for _, row := range rerankResponse.Data {
		if row.Index < 0 || row.Index >= len(documents) || seen[row.Index] {
			return nil, fmt.Errorf("invalid rerank index")
		}
		seen[row.Index] = true
	}

	return &rerankResponse, nil
}

// TranscribeAudio transcribe audio
func (g *GiteeModel) TranscribeAudio(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig) (*ASRResponse, error) {
	return nil, fmt.Errorf("%s, no such method", g.Name())
}

func (g *GiteeModel) TranscribeAudioWithSender(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", g.Name())
}

// AudioSpeech convert text to audio
func (g *GiteeModel) AudioSpeech(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig) (*TTSResponse, error) {
	return nil, fmt.Errorf("%s, no such method", g.Name())
}

func (g *GiteeModel) AudioSpeechWithSender(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", g.Name())
}

type giteeOCRResponse struct {
	Text   string `json:"text_result"`
	Prompt string `json:"prompt"`
}

// OCRFile OCR file
func (g *GiteeModel) OCRFile(modelName *string, content []byte, imageURL *string, apiConfig *APIConfig, ocrConfig *OCRConfig) (*OCRFileResponse, error) {
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if err := g.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if imageURL == nil && content == nil {
		return nil, fmt.Errorf("url or content is required")
	}

	if modelName == nil || *modelName == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if imageURL != nil && *imageURL != "" {
		u, e := neturl.Parse(*imageURL)
		if e != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" || u.User != nil || u.Fragment != "" {
			return nil, fmt.Errorf("invalid document URL")
		}
	}

	resolvedBaseURL, err := g.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL := resolvedBaseURL

	url := fmt.Sprintf("%s/%s", strings.TrimSuffix(baseURL, "/"), g.baseModel.URLSuffix.OCR)

	payload := &bytes.Buffer{}
	writer := multipart.NewWriter(payload)

	if err := writer.WriteField("model", *modelName); err != nil {
		return nil, fmt.Errorf("failed to write model field: %w", err)
	}

	if imageURL != nil && *imageURL != "" {
		if err := writer.WriteField("image", *imageURL); err != nil {
			return nil, fmt.Errorf("failed to write image URL: %w", err)
		}
	} else if content != nil && len(content) > 0 {
		part, err := writer.CreateFormFile("image", "image")
		if err != nil {
			return nil, fmt.Errorf("failed to create image form file: %w", err)
		}
		if _, err = part.Write(content); err != nil {
			return nil, fmt.Errorf("failed to write image content: %w", err)
		}
	} else {
		return nil, fmt.Errorf("image or image URL is required")
	}

	writer.Close()

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), longOpCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "POST", url, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", writer.FormDataContentType())
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := g.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, (20<<20)+1))
	if len(body) > 20<<20 {
		return nil, fmt.Errorf("provider response exceeds limit")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("gitee OCR API error: %s, body: %s", resp.Status, "[redacted]")
	}

	var envelope map[string]json.RawMessage
	var text *string
	if json.Unmarshal(body, &envelope) != nil || json.Unmarshal(envelope["text_result"], &text) != nil || text == nil || (len(envelope["error"]) > 0 && string(envelope["error"]) != "null") {
		return nil, fmt.Errorf("invalid OCR response")
	}
	var giteeResponse giteeOCRResponse
	if err = json.Unmarshal(body, &giteeResponse); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	var ocrResponse = OCRFileResponse{
		Text: &giteeResponse.Text,
	}

	return &ocrResponse, nil
}

type giteeParseFileResponse struct {
	TaskID    string    `json:"task_id"`
	Status    string    `json:"status"`
	CreatedAt int64     `json:"created_at"`
	URLs      giteeURLs `json:"urls"`
}

type giteeURLs struct {
	Get    string `json:"get"`
	Cancel string `json:"cancel"`
}

// ParseFile parse file
func (g *GiteeModel) ParseFile(modelName *string, content []byte, documentURL *string, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*ParseFileResponse, error) {
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if err := g.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if documentURL == nil && content == nil {
		return nil, fmt.Errorf("url or content is required")
	}

	if modelName == nil || *modelName == "" {
		return nil, fmt.Errorf("model name is required")
	}

	if documentURL != nil && *documentURL != "" {
		u, e := neturl.Parse(*documentURL)
		if e != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" || u.User != nil || u.Fragment != "" {
			return nil, fmt.Errorf("invalid document URL")
		}
	}

	resolvedBaseURL, err := g.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	baseURL := resolvedBaseURL

	url := fmt.Sprintf("%s/%s", strings.TrimSuffix(baseURL, "/"), g.baseModel.URLSuffix.DocumentParse)

	payload := &bytes.Buffer{}
	writer := multipart.NewWriter(payload)

	if err := writer.WriteField("model", *modelName); err != nil {
		return nil, fmt.Errorf("failed to write model field: %w", err)
	}

	if documentURL != nil && *documentURL != "" {
		if err := writer.WriteField("file", *documentURL); err != nil {
			return nil, fmt.Errorf("failed to write file URL: %w", err)
		}
	} else if content != nil && len(content) > 0 {
		part, err := writer.CreateFormFile("file", "file")
		if err != nil {
			return nil, fmt.Errorf("failed to create file form file: %w", err)
		}
		if _, err = part.Write(content); err != nil {
			return nil, fmt.Errorf("failed to write file content: %w", err)
		}
	} else {
		return nil, fmt.Errorf("file or file URL is required")
	}

	writer.Close()

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), longOpCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "POST", url, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", writer.FormDataContentType())
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := g.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, (20<<20)+1))
	if len(body) > 20<<20 {
		return nil, fmt.Errorf("provider response exceeds limit")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("gitee OCR API error: %s, body: %s", resp.Status, "[redacted]")
	}

	var giteeParseFileResp giteeParseFileResponse
	if err = json.Unmarshal(body, &giteeParseFileResp); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	var envelope map[string]json.RawMessage
	if json.Unmarshal(body, &envelope) != nil || (envelope["error"] != nil && string(envelope["error"]) != "null") || giteeParseFileResp.Status == "failed" || giteeParseFileResp.Status == "cancelled" {
		return nil, fmt.Errorf("document parse submission failed")
	}

	if strings.TrimSpace(giteeParseFileResp.TaskID) == "" {
		return nil, fmt.Errorf("parse response does not contain a task ID")
	}
	var parseFileResponse = ParseFileResponse{TaskID: giteeParseFileResp.TaskID}

	return &parseFileResponse, nil
}

type giteeGetParseFileResponse struct {
}

func (g *GiteeModel) getParseFile(baseURL *string, apiKey, taskID *string, timeOut time.Duration, count int) (*giteeGetParseFileResponse, error) {
	url := fmt.Sprintf("%s/task/%s/status", strings.TrimSuffix(*baseURL, "/"), *taskID)

	reqBody := map[string]interface{}{}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), longOpCallTimeout)
	defer cancel()

	for i := 0; i < count; i++ {
		req, err := http.NewRequestWithContext(ctx, "GET", url, bytes.NewBuffer(jsonData))
		if err != nil {
			return nil, fmt.Errorf("failed to create request: %w", err)
		}

		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiKey))

		resp, err := g.baseModel.httpClient.Do(req)
		if err != nil {
			return nil, fmt.Errorf("failed to send request: %w", err)
		}

		body, err := io.ReadAll(io.LimitReader(resp.Body, (20<<20)+1))
		if len(body) > 20<<20 {
			return nil, fmt.Errorf("provider response exceeds limit")
		}
		resp.Body.Close()
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

		// Wait before the next poll, but honor context cancellation /
		// longOpCallTimeout instead of blocking uninterruptibly.
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(timeOut):
		}
	}

	// if resp show the file is ok, download it. otherwise, provide timeout info
	return nil, fmt.Errorf("parse task polling did not produce a result")
}

func (g *GiteeModel) ListModels(apiConfig *APIConfig) ([]ListModelResponse, error) {

	resolvedBaseURL, err := g.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, g.baseModel.URLSuffix.Models)

	// Build request body

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")

	resp, err := g.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, (20<<20)+1))
	if len(body) > 20<<20 {
		return nil, fmt.Errorf("provider response exceeds limit")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	// Parse response
	var modelList ModelList
	if err = json.Unmarshal(body, &modelList); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	var envelope map[string]json.RawMessage
	if json.Unmarshal(body, &envelope) != nil || envelope["data"] == nil || string(envelope["data"]) == "null" || (envelope["error"] != nil && string(envelope["error"]) != "null") {
		return nil, fmt.Errorf("invalid model inventory")
	}
	for _, model := range modelList.Models {
		if strings.TrimSpace(model.ID) == "" {
			return nil, fmt.Errorf("invalid model identity")
		}
	}

	return ParseListModel(modelList), nil
}

func (g *GiteeModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	if err := g.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}
	baseURL, err := g.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}

	url := fmt.Sprintf("%s/%s", baseURL, g.baseModel.URLSuffix.Balance)

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("invalid balance endpoint")
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", BearerAuth(apiConfig))

	resp, err := g.baseModel.httpClient.Do(req)
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

	balance, ok := result["balance"].(float64)
	if !ok || result["error"] != nil {
		return nil, fmt.Errorf("invalid balance response")
	}
	if code, exists := result["code"]; exists {
		number, ok := code.(float64)
		if !ok || number != 0 {
			return nil, fmt.Errorf("balance business error")
		}
	}

	var response = map[string]interface{}{
		"balance":  balance,
		"currency": "CNY",
	}

	return response, nil
}

func (g *GiteeModel) CheckConnection(apiConfig *APIConfig) error {
	if err := g.baseModel.APIConfigCheck(apiConfig); err != nil {
		return err
	}

	resolvedBaseURL, err := g.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, g.baseModel.URLSuffix.Status)

	// Build request body
	reqBody := map[string]interface{}{}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return fmt.Errorf("failed to marshal request: %w", err)
	}

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.ApiKey))

	resp, err := g.baseModel.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	_, err = io.Copy(io.Discard, io.LimitReader(resp.Body, 20*1024*1024))
	if err != nil {
		return fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	return nil
}

type giteeTaskListResponse struct {
	Total int             `json:"total"`
	Items []giteeTaskItem `json:"items"`
}

type giteeTaskItem struct {
	TaskID string `json:"task_id"`
	//Output      giteeTaskOutput `json:"output"`
	Status      string        `json:"status"`
	CreatedAt   int64         `json:"created_at"`
	StartedAt   int64         `json:"started_at,omitempty"`
	CompletedAt int64         `json:"completed_at,omitempty"`
	Price       float64       `json:"price"`
	Currency    string        `json:"currency"`
	URLs        giteeTaskURLs `json:"urls"`
}

type giteeTaskOutput struct {
	Segments []giteeSegment `json:"segments"`
}

type giteeSegment struct {
	Index   int    `json:"index"`
	Content string `json:"content"`
}
type giteeTaskURLs struct {
	Get    string `json:"get"`
	Cancel string `json:"cancel"`
}

func (g *GiteeModel) ListTasks(apiConfig *APIConfig) ([]ListTaskStatus, error) {
	if err := g.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	resolvedBaseURL, err := g.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, g.baseModel.URLSuffix.Tasks)

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

	resp, err := g.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	var body []byte
	body, err = io.ReadAll(io.LimitReader(resp.Body, (20<<20)+1))
	if len(body) > 20<<20 {
		return nil, fmt.Errorf("provider response exceeds limit")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	var giteeTaskList giteeTaskListResponse
	if err = json.Unmarshal(body, &giteeTaskList); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	taskListResp := []ListTaskStatus{}
	for _, item := range giteeTaskList.Items {
		taskListResp = append(taskListResp, ListTaskStatus{
			TaskID: item.TaskID,
			Status: item.Status,
		})
	}
	return taskListResp, nil
}

func (g *GiteeModel) ShowTask(taskID string, apiConfig *APIConfig) (*TaskResponse, error) {
	if taskID == "" || taskID == "." || taskID == ".." || strings.ContainsAny(taskID, "/\\?#") {
		return nil, fmt.Errorf("invalid task identity")
	}

	if err := g.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	resolvedBaseURL, err := g.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s/%s/get", resolvedBaseURL, g.baseModel.URLSuffix.Task, taskID)

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

	resp, err := g.baseModel.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, (20<<20)+1))
	if len(body) > 20<<20 {
		return nil, fmt.Errorf("provider response exceeds limit")
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	var taskOutput giteeTaskOutput
	if err = json.Unmarshal(body, &taskOutput); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	var envelope map[string]json.RawMessage
	if json.Unmarshal(body, &envelope) != nil || (envelope["error"] != nil && string(envelope["error"]) != "null") {
		return nil, fmt.Errorf("invalid task result")
	}
	var segments []struct {
		Index   *int    `json:"index"`
		Content *string `json:"content"`
	}
	if json.Unmarshal(envelope["segments"], &segments) != nil || len(segments) == 0 {
		return nil, fmt.Errorf("task result missing segments")
	}
	seen := map[int]bool{}
	for _, segment := range segments {
		if segment.Index == nil || *segment.Index < 0 || seen[*segment.Index] || segment.Content == nil || strings.TrimSpace(*segment.Content) == "" {
			return nil, fmt.Errorf("invalid task segment")
		}
		seen[*segment.Index] = true
	}
	sort.SliceStable(taskOutput.Segments, func(i, j int) bool { return taskOutput.Segments[i].Index < taskOutput.Segments[j].Index })

	taskResp := &TaskResponse{}

	for _, segment := range taskOutput.Segments {
		taskResp.Segments = append(taskResp.Segments, TaskSegment{
			Index:   segment.Index,
			Content: segment.Content,
		})
	}

	return taskResp, nil
}
