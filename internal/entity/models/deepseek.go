package models

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"multirag/internal/logger"
	"net/http"
	"strings"
	"time"
)

// DeepSeekModel implements provider model discovery for DeepSeek.
type DeepSeekModel struct {
	BaseURL    map[string]string
	URLSuffix  URLSuffix
	httpClient *http.Client
}

func NewDeepSeekModel(baseURL map[string]string, urlSuffix URLSuffix) *DeepSeekModel {
	return &DeepSeekModel{
		BaseURL:    baseURL,
		URLSuffix:  urlSuffix,
		httpClient: &http.Client{Timeout: 120 * time.Second},
	}
}

func (m *DeepSeekModel) Name() string {
	return "deepseek"
}

func (m *DeepSeekModel) Chat(modelName, message *string, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	if message == nil {
		return nil, fmt.Errorf("message is nil")
	}
	if apiConfig == nil || apiConfig.APIKey == nil {
		return nil, fmt.Errorf("API key is nil")
	}
	chatModelConfig = normalizeChatConfig(chatModelConfig)

	var region = "default"
	if apiConfig.Region != nil {
		region = *apiConfig.Region
	}

	url := fmt.Sprintf("%s/%s", m.BaseURL[region], m.URLSuffix.Chat)

	// Build request body
	reqBody := map[string]interface{}{
		"model": modelName,
		"messages": []map[string]string{
			{"role": "user", "content": *message},
		},
		"stream":      false,
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

	if chatModelConfig.TopP != nil {
		reqBody["top_p"] = *chatModelConfig.TopP
	}

	if chatModelConfig.Stop != nil {
		reqBody["stop"] = *chatModelConfig.Stop
	}

	if chatModelConfig.Thinking != nil {
		if *chatModelConfig.Thinking {
			var thinkingFlag string
			switch *chatModelConfig.Effort {
			case "none":
				thinkingFlag = "disabled"
				chatModelConfig.Thinking = nil
				break
			case "low":
				thinkingFlag = "disabled"
				chatModelConfig.Thinking = nil
				break
			case "medium":
				thinkingFlag = "disabled"
				chatModelConfig.Thinking = nil
				break
			case "high":
				thinkingFlag = "enabled"
				reqBody["reasoning_effort"] = "high"
				break
			case "default":
				thinkingFlag = "enabled"
				reqBody["reasoning_effort"] = "high"
				break
			case "max":
				thinkingFlag = "enabled"
				reqBody["reasoning_effort"] = "max"
				break
			default:
				return nil, fmt.Errorf("invalid effort level")
			}
			reqBody["thinking"] = map[string]interface{}{
				"type": thinkingFlag,
			}
		} else {
			reqBody["thinking"] = map[string]interface{}{
				"type": "disabled",
			}
		}
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	req, err := http.NewRequest("POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.APIKey))

	resp, err := m.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, string(body))
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
	if chatModelConfig.Thinking != nil && *chatModelConfig.Thinking {
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
		Answer:           &content,
		ReasoningContent: &reasonContent,
	}

	return chatResponse, nil
}

func (m *DeepSeekModel) ChatWithMessages(modelName string, apiKey *string, messages []Message, modelConfig *ChatConfig) (string, error) {
	return "", fmt.Errorf("%s, ChatWithMessages not implemented", m.Name())
}

func (m *DeepSeekModel) ChatStreamly(modelName, apiKey, message *string, genConf map[string]interface{}) (<-chan string, error) {
	return nil, fmt.Errorf("not implemented")
}

func (m *DeepSeekModel) ChatStreamlyWithChannel(modelName, apiKey, message *string, genConf map[string]interface{}, resultChan chan<- string) error {
	return fmt.Errorf("not implemented")
}

func (m *DeepSeekModel) ChatStreamlyWithSender(modelName, message *string, apiConfig *APIConfig, chatModelConfig *ChatConfig, sender func(*string, *string) error) error {
	if message == nil || apiConfig == nil || apiConfig.APIKey == nil {
		return fmt.Errorf("message or API key is nil")
	}
	chatModelConfig = normalizeChatConfig(chatModelConfig)
	var region = "default"
	if apiConfig.Region != nil {
		region = *apiConfig.Region
	}

	url := fmt.Sprintf("%s/chat/completions", m.BaseURL[region])

	// Build request body with streaming enabled
	reqBody := map[string]interface{}{
		"model": modelName,
		"messages": []map[string]string{
			{"role": "user", "content": *message},
		},
		"stream":      false,
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
			var thinkingFlag string
			switch *chatModelConfig.Effort {
			case "none":
				thinkingFlag = "disabled"
				chatModelConfig.Thinking = nil
				break
			case "low":
				thinkingFlag = "disabled"
				chatModelConfig.Thinking = nil
				break
			case "medium":
				thinkingFlag = "disabled"
				chatModelConfig.Thinking = nil
				break
			case "high":
				thinkingFlag = "enabled"
				reqBody["reasoning_effort"] = "high"
				break
			case "default":
				thinkingFlag = "enabled"
				reqBody["reasoning_effort"] = "high"
				break
			case "max":
				thinkingFlag = "enabled"
				reqBody["reasoning_effort"] = "max"
				break
			default:
				return fmt.Errorf("invalid effort level")
			}
			reqBody["thinking"] = map[string]interface{}{
				"type": thinkingFlag,
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

	req, err := http.NewRequest("POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.APIKey))

	resp, err := m.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, string(body))
	}

	// SSE parsing: read line by line
	scanner := bufio.NewScanner(resp.Body)
	for scanner.Scan() {
		line := scanner.Text()
		logger.Info(line)

		// SSE data line starts with "data:"
		if !strings.HasPrefix(line, "data:") {
			continue
		}

		// Extract JSON after "data:"
		data := strings.TrimSpace(line[5:])

		// [DONE] marks the end of stream
		if data == "[DONE]" {
			break
		}

		// Parse the JSON event
		var event map[string]interface{}
		if err = json.Unmarshal([]byte(data), &event); err != nil {
			continue
		}

		choices, ok := event["choices"].([]interface{})
		if !ok || len(choices) == 0 {
			continue
		}

		firstChoice, ok := choices[0].(map[string]interface{})
		if !ok {
			continue
		}

		delta, ok := firstChoice["delta"].(map[string]interface{})
		if !ok {
			continue
		}

		content, ok := delta["content"].(string)
		if ok && content != "" {
			if err := sender(&content, nil); err != nil {
				return err
			}
		}

		reasoningContent, ok := delta["reasoning_content"].(string)
		if ok && reasoningContent != "" {
			if err := sender(nil, &reasoningContent); err != nil {
				return err
			}
		}

		finishReason, ok := firstChoice["finish_reason"].(string)
		if ok && finishReason != "" {
			break
		}
	}

	// Send [DONE] marker for OpenAI compatibility
	endOfStream := "[DONE]"
	if err = sender(&endOfStream, nil); err != nil {
		return err
	}

	return scanner.Err()
}

func (m *DeepSeekModel) EncodeToEmbedding(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([][]float64, error) {
	return nil, fmt.Errorf("not implemented")
}

func (m *DeepSeekModel) ListModels(apiConfig *APIConfig) ([]string, error) {
	if apiConfig == nil || apiConfig.APIKey == nil {
		return nil, fmt.Errorf("API key is nil")
	}
	baseURL, err := resolveModelBaseURL(m.BaseURL, apiConfig.Region)
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequest(http.MethodGet, joinModelURL(baseURL, m.URLSuffix.Models), nil)
	if err != nil {
		return nil, fmt.Errorf("create model list request: %w", err)
	}
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.APIKey))

	resp, err := m.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("list DeepSeek models: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("list DeepSeek models: status %d", resp.StatusCode)
	}

	var payload struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err = json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return nil, fmt.Errorf("decode DeepSeek model list: %w", err)
	}
	models := make([]string, 0, len(payload.Data))
	for _, model := range payload.Data {
		models = append(models, model.ID)
	}
	return models, nil
}

func (m *DeepSeekModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("balance query is not available for DeepSeek")
}

func (m *DeepSeekModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := m.ListModels(apiConfig)
	return err
}

func resolveModelBaseURL(baseURLs map[string]string, region *string) (string, error) {
	regionName := "default"
	if region != nil && *region != "" {
		regionName = *region
	}
	baseURL := strings.TrimRight(baseURLs[regionName], "/")
	if baseURL == "" {
		return "", fmt.Errorf("no base URL configured for region %q", regionName)
	}
	return baseURL, nil
}
