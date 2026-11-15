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
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

type JieKouAIModel struct {
	baseModel BaseModel
}

func NewJieKouAIModel(baseURL map[string]string, urlSuffix URLSuffix) *JieKouAIModel {
	if baseURL != nil {
		copied := make(map[string]string, len(baseURL))
		for region, value := range baseURL {
			copied[region] = value
		}
		baseURL = copied
	}
	// JieKouAI's methods issue requests without a per-call context deadline, so
	// keep an explicit 120s client-level timeout to bound them. Built on the
	// shared transport via NewDriverHTTPClient.
	client := NewDriverHTTPClient()
	client.Timeout = 120 * time.Second
	return &JieKouAIModel{
		baseModel: BaseModel{
			BaseURL:    baseURL,
			URLSuffix:  urlSuffix,
			httpClient: client,
		},
	}
}

func (j *JieKouAIModel) NewInstance(baseURL map[string]string) ModelDriver {
	return NewJieKouAIModel(baseURL, j.baseModel.URLSuffix)
}

func (j *JieKouAIModel) Name() string {
	return "jiekouai"
}

func validateJieKouAIModelName(modelName *string) (string, error) {
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return "", fmt.Errorf("model name is required")
	}
	return strings.TrimSpace(*modelName), nil
}

func (j *JieKouAIModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	if err := j.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}
	apiKey := strings.TrimSpace(*apiConfig.ApiKey)
	if modelName = strings.TrimSpace(modelName); modelName == "" {
		return nil, fmt.Errorf("model name is required")
	}
	if len(messages) == 0 {
		return nil, fmt.Errorf("messages is empty")
	}

	resolvedBaseURL, err := j.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, j.baseModel.URLSuffix.Chat)

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

		// For zai-org/glm-4.5: https://docs.jiekou.ai/docs/models/reference-llm-create-chat-completion
		if chatModelConfig.Thinking != nil {
			if *chatModelConfig.Thinking {
				reqBody["enable_thinking"] = *chatModelConfig.Thinking
			}
		}
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
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", apiKey))
	req.Header.Set("Accept", "application/json")

	resp, err := j.baseModel.httpClient.Do(req)
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

func (j *JieKouAIModel) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, modelConfig *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	if err := j.baseModel.APIConfigCheck(apiConfig); err != nil {
		return err
	}
	apiKey := strings.TrimSpace(*apiConfig.ApiKey)
	if modelName = strings.TrimSpace(modelName); modelName == "" {
		return fmt.Errorf("model name is required")
	}
	if sender == nil {
		return fmt.Errorf("sender is required")
	}
	if len(messages) == 0 {
		return fmt.Errorf("messages is empty")
	}

	resolvedBaseURL, err := j.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return err
	}
	url := fmt.Sprintf("%s/%s", strings.TrimSuffix(resolvedBaseURL, "/"), j.baseModel.URLSuffix.Chat)

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

		if modelConfig.Thinking != nil {
			if *modelConfig.Thinking {
				reqBody["enable_thinking"] = *modelConfig.Thinking
			}
		}
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return fmt.Errorf("failed to marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(apiConfig.RequestContext(), "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", apiKey))
	req.Header.Set("Accept", "text/event-stream")

	resp, err := j.baseModel.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, "[redacted]")
	}

	// SSE parsing: read line by line
	responseHasContent, responseHasTerminal := false, false
	responseDone, responseError := ParseSSEStream[map[string]interface{}](resp.Body, func(rawEvent map[string]interface{}) error {
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
		}

		return nil
	})
	if responseError != nil {
		if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
			return parentErr
		}
		err := responseError

		return fmt.Errorf("failed to scan response body: %w", err)

	}

	// Send [DONE] marker for OpenAI compatibility
	if parentErr := apiConfig.RequestContext().Err(); parentErr != nil {
		return parentErr
	}
	if !responseHasContent || (!responseDone && !responseHasTerminal) {
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

func (j *JieKouAIModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	if embeddingConfig != nil && embeddingConfig.Dimension < 0 {
		return nil, fmt.Errorf("embedding dimension must be nonnegative")
	}

	if err := j.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(texts) == 0 {
		return []EmbeddingData{}, fmt.Errorf("texts is empty")
	}
	model, err := validateJieKouAIModelName(modelName)
	if err != nil {
		return nil, err
	}
	apiKey := strings.TrimSpace(*apiConfig.ApiKey)

	resolvedBaseURL, err := j.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", strings.TrimSuffix(resolvedBaseURL, "/"), j.baseModel.URLSuffix.Embedding)

	reqBody := map[string]interface{}{
		"model": model,
		"input": texts,
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
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", apiKey))

	resp, err := j.baseModel.httpClient.Do(req)
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

	var parsedResponse struct {
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
	if err = json.Unmarshal(body, &parsedResponse); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	if len(parsedResponse.Data) == 0 {
		return nil, fmt.Errorf("failed to parse response")
	}

	var embeddings []EmbeddingData
	for _, embedding := range parsedResponse.Data {
		embeddings = append(embeddings, EmbeddingData{
			Embedding: embedding.Embedding,
			Index:     embedding.Index,
		})
	}

	return embeddings, nil
}

func (j *JieKouAIModel) Rerank(modelName *string, query string, documents []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
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

	if err := j.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	if len(documents) == 0 {
		return &RerankResponse{}, nil
	}
	if strings.TrimSpace(query) == "" {
		return nil, fmt.Errorf("query is required")
	}
	model, err := validateJieKouAIModelName(modelName)
	if err != nil {
		return nil, err
	}
	apiKey := strings.TrimSpace(*apiConfig.ApiKey)

	resolvedBaseURL, err := j.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", strings.TrimSuffix(resolvedBaseURL, "/"), j.baseModel.URLSuffix.Rerank)

	reqBody := map[string]interface{}{
		"model":     model,
		"query":     strings.TrimSpace(query),
		"documents": documents,
	}
	if rerankConfig != nil && rerankConfig.TopN != 0 {
		reqBody["top_n"] = rerankConfig.TopN
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
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", apiKey))

	resp, err := j.baseModel.httpClient.Do(req)
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

	var rerankResp struct {
		Results []struct {
			Index          int     `json:"index"`
			RelevanceScore float64 `json:"relevance_score"`
		} `json:"results"`
	}

	var responseEnvelope map[string]json.RawMessage
	if json.Unmarshal(body, &responseEnvelope) != nil || responseEnvelope == nil || responseEnvelope["error"] != nil {
		return nil, fmt.Errorf("invalid rerank response")
	}

	var numericEnvelope struct {
		Results []RerankResult `json:"results"`
	}
	expectedResults := len(documents)
	if rerankConfig != nil && rerankConfig.TopN > 0 && rerankConfig.TopN < expectedResults {
		expectedResults = rerankConfig.TopN
	}
	if json.Unmarshal(body, &numericEnvelope) != nil || len(numericEnvelope.Results) != expectedResults {
		return nil, fmt.Errorf("invalid rerank count or values")
	}
	seenNumeric := map[int]bool{}
	for _, row := range numericEnvelope.Results {
		if row.Index < 0 || row.Index >= len(documents) || seenNumeric[row.Index] {
			return nil, fmt.Errorf("invalid rerank identity")
		}
		seenNumeric[row.Index] = true
	}
	if err = json.Unmarshal(body, &rerankResp); err != nil {
		return nil, fmt.Errorf("failed to decode response: %w", err)
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

func (j *JieKouAIModel) TranscribeAudio(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig) (*ASRResponse, error) {
	return nil, fmt.Errorf("%s, no such method", j.Name())
}

func (j *JieKouAIModel) TranscribeAudioWithSender(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", j.Name())
}

func (j *JieKouAIModel) AudioSpeech(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig) (*TTSResponse, error) {
	return nil, fmt.Errorf("%s, no such method", j.Name())
}

func (j *JieKouAIModel) AudioSpeechWithSender(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", j.Name())
}

func (j *JieKouAIModel) OCRFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, ocrConfig *OCRConfig) (*OCRFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", j.Name())
}

func (j *JieKouAIModel) ParseFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*ParseFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", j.Name())
}

func (j *JieKouAIModel) ListModels(apiConfig *APIConfig) ([]ListModelResponse, error) {
	if err := j.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}
	apiKey := strings.TrimSpace(*apiConfig.ApiKey)

	resolvedBaseURL, err := j.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("%s/%s", resolvedBaseURL, j.baseModel.URLSuffix.Models)

	req, err := http.NewRequestWithContext(apiConfig.RequestContext(), "GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", apiKey))
	req.Header.Set("Accept", "application/json")

	resp, err := j.baseModel.httpClient.Do(req)
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

	var result struct {
		Code  int             `json:"code"`
		Error json.RawMessage `json:"error"`
		Data  []DSModel       `json:"data"`
	}
	if err = json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}
	if result.Code != 0 || (len(result.Error) > 0 && string(result.Error) != "null") {
		return nil, fmt.Errorf("model discovery business error")
	}
	if result.Data == nil {
		return nil, fmt.Errorf("models response missing data")
	}

	for idx, model := range result.Data {
		model.ID = strings.TrimSpace(model.ID)
		if model.ID == "" {
			return nil, fmt.Errorf("models response contains empty id")
		}
		result.Data[idx] = model
	}

	return ParseListModel(ModelList{Models: result.Data}), nil
}

func (j *JieKouAIModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("%s, no such method", j.Name())
}

func (j *JieKouAIModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := j.ListModels(apiConfig)
	return err
}

func (j *JieKouAIModel) ListTasks(apiConfig *APIConfig) ([]ListTaskStatus, error) {
	return nil, fmt.Errorf("%s no such method", j.Name())
}

func (j *JieKouAIModel) ShowTask(taskID string, apiConfig *APIConfig) (*TaskResponse, error) {
	return nil, fmt.Errorf("%s no such method", j.Name())
}
