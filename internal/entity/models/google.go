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
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"google.golang.org/genai"
)

// GoogleModel implements the Gemini API text-chat and model-list capabilities.
type GoogleModel struct {
	BaseURL   map[string]string
	URLSuffix URLSuffix
}

func NewGoogleModel(baseURL map[string]string, urlSuffix URLSuffix) *GoogleModel {
	return &GoogleModel{BaseURL: baseURL, URLSuffix: urlSuffix}
}

func (g *GoogleModel) Name() string { return "google" }

func googleContext(config *APIConfig) (context.Context, context.CancelFunc) {
	ctx := context.Background()
	if config != nil && config.Context != nil {
		ctx = config.Context
	}
	return context.WithTimeout(ctx, 2*time.Minute)
}

func (g *GoogleModel) client(ctx context.Context, config *APIConfig) (*genai.Client, error) {
	if config == nil || config.APIKey == nil || strings.TrimSpace(*config.APIKey) == "" {
		return nil, errors.New("Google API key is required")
	}
	baseURL := ""
	if config.Region != nil {
		baseURL = strings.TrimSpace(g.BaseURL[*config.Region])
	}
	if baseURL == "" {
		baseURL = strings.TrimSpace(g.BaseURL["default"])
	}
	if baseURL == "" {
		return nil, errors.New("Google base URL is not configured")
	}
	client, err := genai.NewClient(ctx, &genai.ClientConfig{
		APIKey: strings.TrimSpace(*config.APIKey), Backend: genai.BackendGeminiAPI,
		HTTPClient:  &http.Client{Timeout: 2 * time.Minute},
		HTTPOptions: genai.HTTPOptions{BaseURL: baseURL},
	})
	if err != nil {
		return nil, errors.New("Google client configuration is invalid")
	}
	return client, nil
}

// Provider bodies and transport URLs may contain credentials. Preserve useful status
// and cancellation without exposing either in returned errors.
func googleRequestError(ctx context.Context, err error) error {
	if ctx.Err() != nil {
		return ctx.Err()
	}
	var apiError genai.APIError
	if errors.As(err, &apiError) {
		return fmt.Errorf("Google request failed (HTTP %d)", apiError.Code)
	}
	return errors.New("Google request failed")
}

func googleGenerationConfig(config *ChatConfig) *genai.GenerateContentConfig {
	result := &genai.GenerateContentConfig{}
	if config == nil {
		return result
	}
	if config.Thinking != nil {
		result.ThinkingConfig = &genai.ThinkingConfig{IncludeThoughts: *config.Thinking}
		if !*config.Thinking {
			budget := int32(0)
			result.ThinkingConfig.ThinkingBudget = &budget
		}
	}
	if config.MaxTokens != nil {
		result.MaxOutputTokens = int32(*config.MaxTokens)
	}
	if config.Temperature != nil {
		v := float32(*config.Temperature)
		result.Temperature = &v
	}
	if config.TopP != nil {
		v := float32(*config.TopP)
		result.TopP = &v
	}
	if config.Stop != nil {
		result.StopSequences = append([]string(nil), (*config.Stop)...)
	}
	return result
}

func validateGoogleChat(modelName, message *string) error {
	if modelName == nil || strings.TrimSpace(*modelName) == "" {
		return errors.New("Google model name is required")
	}
	if message == nil {
		return errors.New("Google message is required")
	}
	return nil
}

// googleText separates thought parts from answer parts, guarding usage-only chunks.
func googleText(response *genai.GenerateContentResponse, thinking bool) (string, string) {
	var answer, reasoning strings.Builder
	if response == nil || len(response.Candidates) == 0 || response.Candidates[0] == nil || response.Candidates[0].Content == nil {
		return "", ""
	}
	for _, part := range response.Candidates[0].Content.Parts {
		if part == nil {
			continue
		}
		if part.Thought {
			if thinking {
				reasoning.WriteString(part.Text)
			}
		} else {
			answer.WriteString(part.Text)
		}
	}
	return answer.String(), reasoning.String()
}

func (g *GoogleModel) generate(modelName string, contents []*genai.Content, apiConfig *APIConfig, config *ChatConfig, generation *genai.GenerateContentConfig) (*ChatResponse, error) {
	ctx, cancel := googleContext(apiConfig)
	defer cancel()
	client, err := g.client(ctx, apiConfig)
	if err != nil {
		return nil, err
	}
	response, err := client.Models.GenerateContent(ctx, modelName, contents, generation)
	if err != nil {
		return nil, googleRequestError(ctx, err)
	}
	answer, reasoning := googleText(response, config != nil && config.Thinking != nil && *config.Thinking)
	if answer == "" {
		return nil, errors.New("Google returned no text answer")
	}
	return &ChatResponse{Answer: &answer, ReasoningContent: &reasoning}, nil
}

func (g *GoogleModel) Chat(modelName, message *string, apiConfig *APIConfig, config *ChatConfig) (*ChatResponse, error) {
	if err := validateGoogleChat(modelName, message); err != nil {
		return nil, err
	}
	return g.generate(*modelName, []*genai.Content{genai.NewContentFromText(*message, genai.RoleUser)}, apiConfig, config, googleGenerationConfig(config))
}

// ChatWithMessages uses the existing text-history interface, without tool or multimodal expansion.
func (g *GoogleModel) ChatWithMessages(modelName string, apiKey *string, messages []Message, config *ChatConfig) (string, error) {
	if strings.TrimSpace(modelName) == "" || len(messages) == 0 {
		return "", errors.New("Google model name and messages are required")
	}
	generation := googleGenerationConfig(config)
	var contents []*genai.Content
	var system []string
	for _, message := range messages {
		switch message.Role {
		case "system":
			system = append(system, message.Content)
		case "user":
			contents = append(contents, genai.NewContentFromText(message.Content, genai.RoleUser))
		case "assistant", "model":
			contents = append(contents, genai.NewContentFromText(message.Content, genai.RoleModel))
		default:
			return "", errors.New("Google message role is unsupported")
		}
	}
	if len(contents) == 0 {
		return "", errors.New("Google conversation requires a user or model message")
	}
	if len(system) != 0 {
		generation.SystemInstruction = genai.NewContentFromText(strings.Join(system, "\n"), genai.RoleUser)
	}
	response, err := g.generate(modelName, contents, &APIConfig{APIKey: apiKey}, config, generation)
	if err != nil {
		return "", err
	}
	return *response.Answer, nil
}

func (g *GoogleModel) ChatStreamlyWithSender(modelName, message *string, apiConfig *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if err := validateGoogleChat(modelName, message); err != nil {
		return err
	}
	if sender == nil {
		return errors.New("Google stream sender is required")
	}
	ctx, cancel := googleContext(apiConfig)
	defer cancel()
	client, err := g.client(ctx, apiConfig)
	if err != nil {
		return err
	}
	receivedAnswer := false
	for response, err := range client.Models.GenerateContentStream(ctx, *modelName,
		[]*genai.Content{genai.NewContentFromText(*message, genai.RoleUser)}, googleGenerationConfig(config)) {
		if err != nil {
			return googleRequestError(ctx, err)
		}
		answer, reasoning := googleText(response, config != nil && config.Thinking != nil && *config.Thinking)
		if reasoning != "" {
			if err := sender(nil, &reasoning); err != nil {
				return err
			}
		}
		if answer != "" {
			receivedAnswer = true
			if err := sender(&answer, nil); err != nil {
				return err
			}
		}
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	if !receivedAnswer {
		return errors.New("Google returned no text answer")
	}
	done := "[DONE]"
	return sender(&done, nil)
}

// The legacy channel-only API cannot report asynchronous provider errors safely.
func (g *GoogleModel) ChatStreamly(modelName, apiKey, message *string, genConf map[string]interface{}) (<-chan string, error) {
	return nil, errors.New("Google channel-only streaming is unsupported; use the sender interface")
}
func (g *GoogleModel) ChatStreamlyWithChannel(modelName, apiKey, message *string, genConf map[string]interface{}, resultChan chan<- string) error {
	return errors.New("Google channel-only streaming is unsupported; use the sender interface")
}
func (g *GoogleModel) Encode(modelName *string, texts []string, apiConfig *APIConfig, config *EmbeddingConfig) ([][]float64, error) {
	return nil, errors.New("Google embeddings are unsupported by this text-chat provider")
}
func (g *GoogleModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, errors.New("Google balance lookup is unsupported")
}

func (g *GoogleModel) ListModels(apiConfig *APIConfig) ([]string, error) {
	ctx, cancel := googleContext(apiConfig)
	defer cancel()
	client, err := g.client(ctx, apiConfig)
	if err != nil {
		return nil, err
	}
	names := []string{}
	seen := map[string]bool{}
	token := ""
	for {
		page, err := client.Models.List(ctx, &genai.ListModelsConfig{PageToken: token})
		if err != nil {
			return nil, googleRequestError(ctx, err)
		}
		for _, model := range page.Items {
			if model != nil {
				names = append(names, model.Name)
			}
		}
		if page.NextPageToken == "" {
			return names, nil
		}
		if seen[page.NextPageToken] {
			return nil, errors.New("Google returned a repeated model page token")
		}
		seen[page.NextPageToken] = true
		token = page.NextPageToken
	}
}
func (g *GoogleModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := g.ListModels(apiConfig)
	return err
}

func (m *GoogleModel) Rerank(modelName *string, query string, texts []string, apiConfig *APIConfig) ([]float64, error) {
	return nil, fmt.Errorf("%s: rerank is not supported", m.Name())
}
