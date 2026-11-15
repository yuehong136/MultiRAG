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
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"strings"

	"google.golang.org/genai"
)

// Google SDK decoding accepts JSON null as a numeric zero and panics on a
// top-level null response. Check the original nonstream body before the SDK
// discards that information, then let the SDK perform its normal conversion.
type googleResponseTransport struct{ base http.RoundTripper }

func (g googleResponseTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	response, err := g.base.RoundTrip(request)
	if err != nil || response == nil || response.StatusCode != http.StatusOK || strings.Contains(request.URL.Path, ":streamGenerateContent") {
		return response, err
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, 20*1024*1024+1))
	response.Body.Close()
	if err != nil {
		return nil, err
	}
	if len(body) > 20*1024*1024 {
		return nil, fmt.Errorf("Google response exceeds limit")
	}
	var object map[string]json.RawMessage
	if err := json.Unmarshal(body, &object); err != nil || object == nil {
		return nil, fmt.Errorf("invalid Google response")
	}
	if raw, ok := object["error"]; ok && string(raw) != "null" {
		var failure struct {
			Code int `json:"code"`
		}
		_ = json.Unmarshal(raw, &failure)
		return nil, genai.APIError{Code: failure.Code}
	}
	if strings.HasSuffix(request.URL.Path, ":generateContent") {
		var candidates []struct {
			Content *struct {
				Parts []json.RawMessage `json:"parts"`
			} `json:"content"`
		}
		if err := json.Unmarshal(object["candidates"], &candidates); err != nil || len(candidates) == 0 || candidates[0].Content == nil || len(candidates[0].Content.Parts) == 0 {
			return nil, fmt.Errorf("empty Google chat response")
		}
		for _, part := range candidates[0].Content.Parts {
			var value map[string]json.RawMessage
			if err := json.Unmarshal(part, &value); err != nil || value == nil {
				return nil, fmt.Errorf("invalid Google response part")
			}
		}
	}
	if request.Method == http.MethodGet && strings.HasSuffix(request.URL.Path, "/models") {
		raw, ok := object["models"]
		if !ok || string(raw) == "null" {
			return nil, fmt.Errorf("Google response has no inventory")
		}
		var models []json.RawMessage
		if err := json.Unmarshal(raw, &models); err != nil {
			return nil, fmt.Errorf("invalid Google inventory")
		}
		for _, raw := range models {
			var model struct {
				Name        string `json:"name"`
				DisplayName string `json:"displayName"`
			}
			if err := json.Unmarshal(raw, &model); err != nil || strings.TrimSpace(model.Name) == "" && strings.TrimSpace(model.DisplayName) == "" {
				return nil, fmt.Errorf("invalid Google model identity")
			}
		}
	}
	if strings.HasSuffix(request.URL.Path, ":batchEmbedContents") || strings.HasSuffix(request.URL.Path, ":embedContent") {
		raw, ok := object["embeddings"]
		if !ok || string(raw) == "null" {
			return nil, fmt.Errorf("Google response has no embeddings")
		}
		var rows []struct {
			Values []*float64 `json:"values"`
		}
		if err := json.Unmarshal(raw, &rows); err != nil || rows == nil {
			return nil, fmt.Errorf("invalid Google embeddings")
		}
		for _, row := range rows {
			if len(row.Values) == 0 {
				return nil, fmt.Errorf("empty Google embedding")
			}
			nonzero := false
			for _, value := range row.Values {
				if value == nil || math.IsNaN(*value) || math.IsInf(*value, 0) || math.Abs(*value) > math.MaxFloat32 {
					return nil, fmt.Errorf("invalid Google embedding value")
				}
				nonzero = nonzero || *value != 0
			}
			if !nonzero {
				return nil, fmt.Errorf("zero Google embedding")
			}
		}
	}
	response.Body = io.NopCloser(bytes.NewReader(body))
	return response, nil
}

type googleModelPage struct {
	items         []DSModel
	nextPageToken string
}

func collectGoogleModelNames(ctx context.Context, listPage func(context.Context, string) (googleModelPage, error)) ([]ListModelResponse, error) {
	var models []DSModel
	pageToken := ""
	seen := make(map[string]bool)

	for {
		if seen[pageToken] {
			return nil, fmt.Errorf("Google model list repeated a page token")
		}
		seen[pageToken] = true
		page, err := listPage(ctx, pageToken)
		if err != nil {
			return nil, err
		}

		models = append(models, page.items...)
		if page.nextPageToken == "" {
			return ParseListModel(ModelList{Models: models}), nil
		}
		pageToken = page.nextPageToken
	}
}

var googleListModels = func(ctx context.Context, config *genai.ClientConfig) ([]ListModelResponse, error) {
	client, err := genai.NewClient(ctx, config)
	if err != nil {
		return nil, err
	}

	return collectGoogleModelNames(ctx, func(ctx context.Context, pageToken string) (googleModelPage, error) {
		models, err := client.Models.List(ctx, &genai.ListModelsConfig{PageToken: pageToken})
		if err != nil {
			if ctx.Err() != nil {
				return googleModelPage{}, ctx.Err()
			}
			var apiErr genai.APIError
			if errors.As(err, &apiErr) {
				return googleModelPage{}, fmt.Errorf("Google request failed with status %d", apiErr.Code)
			}
			return googleModelPage{}, fmt.Errorf("Google model list request failed")
		}

		var modelNames []DSModel
		for _, m := range models.Items {
			modelName := strings.TrimSpace(m.DisplayName)
			if modelName == "" {
				modelName = strings.TrimSpace(m.Name)
			}
			if modelName != "" {
				modelNames = append(modelNames, DSModel{
					ID:      modelName,
					OwnedBy: "Google",
				})
			}
		}
		return googleModelPage{items: modelNames, nextPageToken: models.NextPageToken}, nil
	})
}

// GoogleModel implements ModelDriver for Google AI
type GoogleModel struct {
	baseModel BaseModel
}

// NewGoogleModel creates a new Google AI model instance
func NewGoogleModel(baseURL map[string]string, urlSuffix URLSuffix) *GoogleModel {
	if baseURL != nil {
		copied := make(map[string]string, len(baseURL))
		for region, value := range baseURL {
			copied[region] = value
		}
		baseURL = copied
	}
	return &GoogleModel{
		baseModel: BaseModel{
			BaseURL:    baseURL,
			URLSuffix:  urlSuffix,
			httpClient: NewDriverHTTPClient(),
		},
	}
}

func (g *GoogleModel) NewInstance(baseURL map[string]string) ModelDriver {
	return NewGoogleModel(baseURL, g.baseModel.URLSuffix)
}

func (g *GoogleModel) Name() string {
	return "google"
}

func (g *GoogleModel) clientConfig(apiKey string, apiConfig *APIConfig) *genai.ClientConfig {
	client := *g.baseModel.httpClient
	client.Transport = googleResponseTransport{base: client.Transport}
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	return &genai.ClientConfig{APIKey: apiKey, Backend: genai.BackendGeminiAPI, HTTPClient: &client, HTTPOptions: genai.HTTPOptions{BaseURL: g.baseURL(apiConfig)}}
}

func (g *GoogleModel) baseURL(apiConfig *APIConfig) string {
	baseURL, err := g.baseModel.GetBaseURL(apiConfig)
	if err != nil {
		defaultConfig := &APIConfig{}
		if apiConfig != nil {
			defaultConfig.BaseURL = apiConfig.BaseURL
		}
		baseURL, err = g.baseModel.GetBaseURL(defaultConfig)
		if err != nil {
			return ""
		}
	}
	return strings.TrimSpace(baseURL)
}

func (g *GoogleModel) ChatWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	if err := g.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}
	if strings.TrimSpace(modelName) == "" {
		return nil, fmt.Errorf("model name is empty")
	}

	if len(messages) == 0 {
		return nil, fmt.Errorf("messages is empty")
	}

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()
	if endpoint := g.baseURL(apiConfig); endpoint != "" {
		parsed, err := url.Parse(endpoint)
		if err != nil || parsed.Hostname() == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
			return nil, fmt.Errorf("invalid Google endpoint")
		}
	}

	client, err := genai.NewClient(ctx, g.clientConfig(strings.TrimSpace(*apiConfig.ApiKey), apiConfig))
	if err != nil {
		return nil, err
	}

	generation := &genai.GenerateContentConfig{}
	if chatModelConfig != nil {
		if chatModelConfig.Temperature != nil {
			value := float32(*chatModelConfig.Temperature)
			generation.Temperature = &value
		}
		if chatModelConfig.TopP != nil {
			value := float32(*chatModelConfig.TopP)
			generation.TopP = &value
		}
		if chatModelConfig.MaxTokens != nil {
			generation.MaxOutputTokens = int32(*chatModelConfig.MaxTokens)
		}
		if chatModelConfig.Stop != nil {
			generation.StopSequences = append([]string(nil), (*chatModelConfig.Stop)...)
		}
		if chatModelConfig.Thinking != nil {
			generation.ThinkingConfig = &genai.ThinkingConfig{IncludeThoughts: *chatModelConfig.Thinking}
			if !*chatModelConfig.Thinking {
				budget := int32(0)
				generation.ThinkingConfig.ThinkingBudget = &budget
			}
		}
	}
	var contents []*genai.Content
	for _, msg := range messages {
		var role genai.Role = genai.RoleUser
		switch msg.Role {
		case "user", "system":
		case "assistant", "model":
			role = genai.RoleModel
		default:
			return nil, fmt.Errorf("unsupported message role")
		}
		var parts []*genai.Part
		var rawParts []any
		switch value := msg.Content.(type) {
		case string:
			parts = append(parts, genai.NewPartFromText(value))
		case []interface{}:
			rawParts = value
		case []map[string]interface{}:
			for _, item := range value {
				rawParts = append(rawParts, item)
			}
		default:
			return nil, fmt.Errorf("unsupported message content")
		}
		for _, item := range rawParts {
			part, ok := item.(map[string]interface{})
			if !ok {
				return nil, fmt.Errorf("invalid multimodal part")
			}
			kind, _ := part["type"].(string)
			switch kind {
			case "text":
				text, ok := part["text"].(string)
				if !ok {
					return nil, fmt.Errorf("text part is not text")
				}
				parts = append(parts, genai.NewPartFromText(text))
			case "image_url":
				image, ok := part["image_url"].(map[string]interface{})
				if !ok {
					return nil, fmt.Errorf("invalid image part")
				}
				raw, ok := image["url"].(string)
				if !ok || strings.TrimSpace(raw) == "" {
					return nil, fmt.Errorf("image URL is required")
				}
				if strings.HasPrefix(raw, "data:") {
					header, data, ok := strings.Cut(strings.TrimPrefix(raw, "data:"), ",")
					if !ok || !strings.HasSuffix(header, ";base64") {
						return nil, fmt.Errorf("invalid image data URL")
					}
					mime := strings.TrimSuffix(header, ";base64")
					if !strings.HasPrefix(mime, "image/") {
						return nil, fmt.Errorf("unsupported image MIME type")
					}
					content, err := base64.StdEncoding.DecodeString(data)
					if err != nil || len(content) == 0 {
						return nil, fmt.Errorf("invalid image bytes")
					}
					parts = append(parts, genai.NewPartFromBytes(content, mime))
				} else {
					parsed, err := url.Parse(raw)
					if err != nil || parsed.Hostname() == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.User != nil || parsed.Fragment != "" {
						return nil, fmt.Errorf("invalid image URL")
					}
					mime := "image/jpeg"
					for _, ext := range []string{"png", "webp", "gif"} {
						if strings.HasSuffix(strings.ToLower(parsed.Path), "."+ext) {
							mime = "image/" + ext
						}
					}
					parts = append(parts, genai.NewPartFromURI(raw, mime))
				}
			default:
				return nil, fmt.Errorf("unsupported multimodal part")
			}
		}
		if len(parts) == 0 {
			return nil, fmt.Errorf("message content is empty")
		}
		if msg.Role == "system" {
			if generation.SystemInstruction == nil {
				generation.SystemInstruction = &genai.Content{}
			}
			generation.SystemInstruction.Parts = append(generation.SystemInstruction.Parts, parts...)
			continue
		}
		contents = append(contents, genai.NewContentFromParts(parts, role))
	}
	if len(contents) == 0 {
		return nil, fmt.Errorf("user messages are required")
	}

	// Generate content (non-streaming)
	response, err := client.Models.GenerateContent(ctx, modelName, contents, generation)
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		var apiErr genai.APIError
		if errors.As(err, &apiErr) {
			return nil, fmt.Errorf("Google request failed with status %d", apiErr.Code)
		}
		return nil, fmt.Errorf("Google chat request failed")
	}

	if response == nil || len(response.Candidates) == 0 || response.Candidates[0] == nil || response.Candidates[0].Content == nil {
		return nil, fmt.Errorf("empty Google chat response")
	}
	answer, reason := "", ""
	for _, part := range response.Candidates[0].Content.Parts {
		if part == nil {
			continue
		}
		if part.Thought {
			if chatModelConfig != nil && chatModelConfig.Thinking != nil && *chatModelConfig.Thinking {
				reason += part.Text
			}
		} else {
			answer += part.Text
		}
	}
	if answer == "" {
		return nil, fmt.Errorf("empty Google chat response")
	}
	return &ChatResponse{Answer: &answer, ReasonContent: &reason}, nil
}

func (g *GoogleModel) ChatStreamlyWithSender(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("sender is required")
	}

	if err := g.baseModel.APIConfigCheck(apiConfig); err != nil {
		return err
	}

	if len(messages) == 0 {
		return fmt.Errorf("messages is empty")
	}
	if strings.TrimSpace(modelName) == "" {
		return fmt.Errorf("model name is empty")
	}
	if sender == nil {
		return fmt.Errorf("sender is nil")
	}

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), streamCallTimeout)
	defer cancel()
	if endpoint := g.baseURL(apiConfig); endpoint != "" {
		parsed, err := url.Parse(endpoint)
		if err != nil || parsed.Hostname() == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
			return fmt.Errorf("invalid Google endpoint")
		}
	}

	client, err := genai.NewClient(ctx, g.clientConfig(strings.TrimSpace(*apiConfig.ApiKey), apiConfig))
	if err != nil {
		return err
	}

	generation := &genai.GenerateContentConfig{}
	if chatModelConfig != nil {
		if chatModelConfig.Temperature != nil {
			value := float32(*chatModelConfig.Temperature)
			generation.Temperature = &value
		}
		if chatModelConfig.TopP != nil {
			value := float32(*chatModelConfig.TopP)
			generation.TopP = &value
		}
		if chatModelConfig.MaxTokens != nil {
			generation.MaxOutputTokens = int32(*chatModelConfig.MaxTokens)
		}
		if chatModelConfig.Stop != nil {
			generation.StopSequences = append([]string(nil), (*chatModelConfig.Stop)...)
		}
		if chatModelConfig.Thinking != nil {
			generation.ThinkingConfig = &genai.ThinkingConfig{IncludeThoughts: *chatModelConfig.Thinking}
			if !*chatModelConfig.Thinking {
				budget := int32(0)
				generation.ThinkingConfig.ThinkingBudget = &budget
			}
		}
	}
	var contents []*genai.Content
	for _, msg := range messages {
		var role genai.Role = genai.RoleUser
		switch msg.Role {
		case "user", "system":
		case "assistant", "model":
			role = genai.RoleModel
		default:
			return fmt.Errorf("unsupported message role")
		}
		var parts []*genai.Part
		var rawParts []any
		switch value := msg.Content.(type) {
		case string:
			parts = append(parts, genai.NewPartFromText(value))
		case []interface{}:
			rawParts = value
		case []map[string]interface{}:
			for _, item := range value {
				rawParts = append(rawParts, item)
			}
		default:
			return fmt.Errorf("unsupported message content")
		}
		for _, item := range rawParts {
			part, ok := item.(map[string]interface{})
			if !ok {
				return fmt.Errorf("invalid multimodal part")
			}
			kind, _ := part["type"].(string)
			switch kind {
			case "text":
				text, ok := part["text"].(string)
				if !ok {
					return fmt.Errorf("text part is not text")
				}
				parts = append(parts, genai.NewPartFromText(text))
			case "image_url":
				image, ok := part["image_url"].(map[string]interface{})
				if !ok {
					return fmt.Errorf("invalid image part")
				}
				raw, ok := image["url"].(string)
				if !ok || strings.TrimSpace(raw) == "" {
					return fmt.Errorf("image URL is required")
				}
				if strings.HasPrefix(raw, "data:") {
					header, data, ok := strings.Cut(strings.TrimPrefix(raw, "data:"), ",")
					if !ok || !strings.HasSuffix(header, ";base64") {
						return fmt.Errorf("invalid image data URL")
					}
					mime := strings.TrimSuffix(header, ";base64")
					if !strings.HasPrefix(mime, "image/") {
						return fmt.Errorf("unsupported image MIME type")
					}
					content, err := base64.StdEncoding.DecodeString(data)
					if err != nil || len(content) == 0 {
						return fmt.Errorf("invalid image bytes")
					}
					parts = append(parts, genai.NewPartFromBytes(content, mime))
				} else {
					parsed, err := url.Parse(raw)
					if err != nil || parsed.Hostname() == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.User != nil || parsed.Fragment != "" {
						return fmt.Errorf("invalid image URL")
					}
					mime := "image/jpeg"
					for _, ext := range []string{"png", "webp", "gif"} {
						if strings.HasSuffix(strings.ToLower(parsed.Path), "."+ext) {
							mime = "image/" + ext
						}
					}
					parts = append(parts, genai.NewPartFromURI(raw, mime))
				}
			default:
				return fmt.Errorf("unsupported multimodal part")
			}
		}
		if len(parts) == 0 {
			return fmt.Errorf("message content is empty")
		}
		if msg.Role == "system" {
			if generation.SystemInstruction == nil {
				generation.SystemInstruction = &genai.Content{}
			}
			generation.SystemInstruction.Parts = append(generation.SystemInstruction.Parts, parts...)
			continue
		}
		contents = append(contents, genai.NewContentFromParts(parts, role))
	}
	if len(contents) == 0 {
		return fmt.Errorf("user messages are required")
	}

	answered, completed := false, false
	for response, err := range client.Models.GenerateContentStream(ctx, modelName, contents, generation) {
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			var apiErr genai.APIError
			if errors.As(err, &apiErr) {
				return fmt.Errorf("Google request failed with status %d", apiErr.Code)
			}
			return fmt.Errorf("Google stream request failed")
		}
		if response == nil || len(response.Candidates) == 0 || response.Candidates[0] == nil {
			continue
		}
		candidate := response.Candidates[0]
		if candidate.FinishReason == genai.FinishReasonStop {
			completed = true
		}
		if candidate.Content == nil {
			continue
		}
		for _, part := range candidate.Content.Parts {
			if part == nil || part.Text == "" {
				continue
			}
			text := part.Text
			if part.Thought {
				if chatModelConfig != nil && chatModelConfig.Thinking != nil && *chatModelConfig.Thinking {
					if err := sender(nil, &text); err != nil {
						return err
					}
				}
			} else {
				answered = true
				if err := sender(&text, nil); err != nil {
					return err
				}
			}
			if err := ctx.Err(); err != nil {
				return err
			}
		}
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if !answered || !completed {
		return fmt.Errorf("incomplete Google chat stream")
	}
	done := "[DONE]"
	return sender(&done, nil)
}

func (g *GoogleModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	if len(texts) > 0 && (modelName == nil || strings.TrimSpace(*modelName) == "") {
		return nil, fmt.Errorf("model name is required")
	}

	if err := g.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return nil, fmt.Errorf("model name is required")
	}
	if len(texts) == 0 {
		return nil, fmt.Errorf("texts is empty")
	}
	if embeddingConfig != nil && (embeddingConfig.Dimension < 0 || embeddingConfig.Dimension > math.MaxInt32) {
		return nil, fmt.Errorf("invalid embedding dimension")
	}
	for _, text := range texts {
		if strings.TrimSpace(text) == "" {
			return nil, fmt.Errorf("embedding text is empty")
		}
	}

	ctx, cancel := context.WithTimeout(apiConfig.RequestContext(), nonStreamCallTimeout)
	defer cancel()

	if endpoint := g.baseURL(apiConfig); endpoint != "" {
		parsed, err := url.Parse(endpoint)
		if err != nil || parsed.Hostname() == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
			return nil, fmt.Errorf("invalid Google endpoint")
		}
	}

	client, err := genai.NewClient(ctx, g.clientConfig(strings.TrimSpace(*apiConfig.ApiKey), apiConfig))
	if err != nil {
		return nil, fmt.Errorf("failed to create client: %w", err)
	}

	contents := make([]*genai.Content, len(texts))
	for i, text := range texts {
		contents[i] = genai.NewContentFromText(text, genai.RoleUser)
	}

	var cfg *genai.EmbedContentConfig
	if embeddingConfig != nil && embeddingConfig.Dimension > 0 {
		dim := int32(embeddingConfig.Dimension)
		cfg = &genai.EmbedContentConfig{OutputDimensionality: &dim}
	}

	resp, err := client.Models.EmbedContent(ctx, *modelName, contents, cfg)
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		var apiErr genai.APIError
		if errors.As(err, &apiErr) {
			return nil, fmt.Errorf("Google request failed with status %d", apiErr.Code)
		}
		return nil, fmt.Errorf("Google embedding request failed")
	}

	if resp == nil {
		return nil, fmt.Errorf("empty Google embedding response")
	}
	if len(resp.Embeddings) != len(texts) {
		return nil, fmt.Errorf("expected %d embeddings, got %d", len(texts), len(resp.Embeddings))
	}

	result := make([]EmbeddingData, len(resp.Embeddings))
	dimension := 0
	for i, emb := range resp.Embeddings {
		if emb == nil || len(emb.Values) == 0 {
			return nil, fmt.Errorf("empty Google embedding vector")
		}
		if dimension == 0 {
			dimension = len(emb.Values)
		}
		if len(emb.Values) != dimension {
			return nil, fmt.Errorf("inconsistent Google embedding dimensions")
		}
		if embeddingConfig != nil && embeddingConfig.Dimension > 0 && len(emb.Values) != embeddingConfig.Dimension {
			return nil, fmt.Errorf("Google embedding dimension mismatch")
		}
		vec := make([]float64, len(emb.Values))
		for j, v := range emb.Values {
			vec[j] = float64(v)
		}
		result[i] = EmbeddingData{
			Embedding: vec,
			Index:     i,
		}
	}

	return result, nil
}

func (g *GoogleModel) ListModels(apiConfig *APIConfig) ([]ListModelResponse, error) {
	if err := g.baseModel.APIConfigCheck(apiConfig); err != nil {
		return nil, err
	}

	return googleListModels(apiConfig.RequestContext(), g.clientConfig(strings.TrimSpace(*apiConfig.ApiKey), apiConfig))
}

func (g *GoogleModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("no such method")
}

func (g *GoogleModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := g.ListModels(apiConfig)
	return err
}

// Rerank calculates similarity scores between query and documents
func (g *GoogleModel) Rerank(modelName *string, query string, documents []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return nil, fmt.Errorf("%s, Rerank not implemented", g.Name())
}

// TranscribeAudio transcribe audio
func (g *GoogleModel) TranscribeAudio(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig) (*ASRResponse, error) {
	return nil, fmt.Errorf("%s, no such method", g.Name())
}

func (g *GoogleModel) TranscribeAudioWithSender(modelName *string, file *string, apiConfig *APIConfig, asrConfig *ASRConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", g.Name())
}

// AudioSpeech convert text to audio
func (g *GoogleModel) AudioSpeech(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig) (*TTSResponse, error) {
	return nil, fmt.Errorf("%s, no such method", g.Name())
}

func (g *GoogleModel) AudioSpeechWithSender(modelName *string, audioContent *string, apiConfig *APIConfig, ttsConfig *TTSConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("%s, no such method", g.Name())
}

// OCRFile OCR file
func (g *GoogleModel) OCRFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, ocrConfig *OCRConfig) (*OCRFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", g.Name())
}

// ParseFile parse file
func (g *GoogleModel) ParseFile(modelName *string, content []byte, url *string, apiConfig *APIConfig, parseFileConfig *ParseFileConfig) (*ParseFileResponse, error) {
	return nil, fmt.Errorf("%s, no such method", g.Name())
}

func (g *GoogleModel) ListTasks(apiConfig *APIConfig) ([]ListTaskStatus, error) {
	return nil, fmt.Errorf("%s, no such method", g.Name())
}

func (g *GoogleModel) ShowTask(taskID string, apiConfig *APIConfig) (*TaskResponse, error) {
	return nil, fmt.Errorf("%s, no such method", g.Name())
}
