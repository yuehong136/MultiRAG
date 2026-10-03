package models

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// MoonshotModel implements Moonshot text chat, reasoning streams and discovery.
type MoonshotModel struct {
	BaseURL    map[string]string
	URLSuffix  URLSuffix
	httpClient *http.Client
}

func NewMoonshotModel(baseURL map[string]string, urlSuffix URLSuffix) *MoonshotModel {
	return &MoonshotModel{
		BaseURL:    baseURL,
		URLSuffix:  urlSuffix,
		httpClient: &http.Client{Timeout: 120 * time.Second},
	}
}

func (m *MoonshotModel) Name() string {
	return "moonshot"
}

func (m *MoonshotModel) Chat(modelName, message *string, apiConfig *APIConfig, modelConfig *ChatConfig) (*ChatResponse, error) {
	if modelName == nil || message == nil {
		return nil, fmt.Errorf("moonshot: model name and message are required")
	}
	return m.chat(*modelName, []Message{{Role: "user", Content: *message}}, apiConfig, modelConfig)
}

func (m *MoonshotModel) ChatWithMessages(modelName string, apiKey *string, messages []Message, modelConfig *ChatConfig) (string, error) {
	response, err := m.chat(modelName, messages, &APIConfig{APIKey: apiKey}, modelConfig)
	if err != nil {
		return "", err
	}
	return *response.Answer, nil
}

func (m *MoonshotModel) ChatStreamly(modelName, apiKey, message *string, genConf map[string]interface{}) (<-chan string, error) {
	return nil, fmt.Errorf("moonshot: channel-only streaming is unsupported; use the sender interface")
}

func (m *MoonshotModel) ChatStreamlyWithChannel(modelName, apiKey, message *string, genConf map[string]interface{}, resultChan chan<- string) error {
	return fmt.Errorf("moonshot: channel-only streaming is unsupported; use the sender interface")
}

func moonshotChatBody(modelName string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	if strings.TrimSpace(modelName) == "" {
		return nil, fmt.Errorf("moonshot: model name is required")
	}
	if len(messages) == 0 {
		return nil, fmt.Errorf("moonshot: at least one message is required")
	}
	apiMessages := make([]map[string]string, 0, len(messages))
	for _, message := range messages {
		if message.Role == "" {
			return nil, fmt.Errorf("moonshot: message role is required")
		}
		apiMessages = append(apiMessages, map[string]string{"role": message.Role, "content": message.Content})
	}
	// The entry point determines the response protocol, regardless of config.Stream.
	body := map[string]interface{}{"model": modelName, "messages": apiMessages, "stream": stream}
	if config == nil {
		return body, nil
	}
	if config.MaxTokens != nil {
		body["max_tokens"] = *config.MaxTokens
	}
	if config.Temperature != nil {
		body["temperature"] = *config.Temperature
	}
	if config.TopP != nil {
		body["top_p"] = *config.TopP
	}
	if config.DoSample != nil {
		body["do_sample"] = *config.DoSample
	}
	if config.Stop != nil {
		body["stop"] = *config.Stop
	}
	if config.Thinking != nil {
		mode := "disabled"
		if *config.Thinking {
			mode = "enabled"
		}
		body["thinking"] = map[string]string{"type": mode}
	}
	return body, nil
}

func (m *MoonshotModel) requestChat(body map[string]interface{}, config *APIConfig) (*http.Response, error) {
	if config == nil || config.APIKey == nil || strings.TrimSpace(*config.APIKey) == "" {
		return nil, fmt.Errorf("moonshot: API key is required")
	}
	if m.URLSuffix.Chat == "" {
		return nil, fmt.Errorf("moonshot: chat endpoint suffix is missing")
	}
	baseURL, err := resolveModelBaseURL(m.BaseURL, config.Region)
	if err != nil {
		return nil, err
	}
	data, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("moonshot: encode request: %w", err)
	}
	req, err := http.NewRequestWithContext(requestContext(config), http.MethodPost, joinModelURL(baseURL, m.URLSuffix.Chat), bytes.NewReader(data))
	if err != nil {
		return nil, fmt.Errorf("moonshot: create request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*config.APIKey))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	if body["stream"] == true {
		req.Header.Set("Accept", "text/event-stream")
	}
	resp, err := m.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("moonshot: send request: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("moonshot: request failed (HTTP %d)", resp.StatusCode)
	}
	return resp, nil
}

func (m *MoonshotModel) chat(modelName string, messages []Message, apiConfig *APIConfig, config *ChatConfig) (*ChatResponse, error) {
	body, err := moonshotChatBody(modelName, messages, config, false)
	if err != nil {
		return nil, err
	}
	resp, err := m.requestChat(body, apiConfig)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result aliyunChatResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("moonshot: decode response: %w", err)
	}
	if len(result.Error) != 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("moonshot: provider returned an error")
	}
	if len(result.Choices) == 0 || result.Choices[0].Message.Content == nil || *result.Choices[0].Message.Content == "" {
		return nil, fmt.Errorf("moonshot: no text answer")
	}
	message := result.Choices[0].Message
	// Reasoning is optional, including when the provider enables thinking by default.
	if message.ReasoningContent != nil {
		reasoning := strings.TrimPrefix(*message.ReasoningContent, "\n")
		message.ReasoningContent = &reasoning
	}
	return &ChatResponse{Answer: message.Content, ReasoningContent: message.ReasoningContent}, nil
}

func (m *MoonshotModel) ChatStreamlyWithSender(modelName, message *string, apiConfig *APIConfig, modelConfig *ChatConfig, sender func(*string, *string) error) error {
	if modelName == nil || message == nil || sender == nil {
		return fmt.Errorf("moonshot: model name, message and sender are required")
	}
	body, err := moonshotChatBody(*modelName, []Message{{Role: "user", Content: *message}}, modelConfig, true)
	if err != nil {
		return err
	}
	resp, err := m.requestChat(body, apiConfig)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	receivedAnswer := false
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		data := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if data == "" {
			continue
		}
		if data == "[DONE]" {
			if !receivedAnswer {
				return fmt.Errorf("moonshot: stream returned no text answer")
			}
			return sender(&data, nil)
		}
		var event struct {
			Choices []struct {
				Delta struct {
					Content          *string `json:"content"`
					ReasoningContent *string `json:"reasoning_content"`
				} `json:"delta"`
			} `json:"choices"`
			Error json.RawMessage `json:"error"`
		}
		if err := json.Unmarshal([]byte(data), &event); err != nil {
			return fmt.Errorf("moonshot: decode stream: %w", err)
		}
		if len(event.Error) != 0 && string(event.Error) != "null" {
			return fmt.Errorf("moonshot: provider stream error")
		}
		if len(event.Choices) == 0 {
			continue
		}
		delta := event.Choices[0].Delta
		if delta.ReasoningContent != nil && *delta.ReasoningContent != "" {
			if err := sender(nil, delta.ReasoningContent); err != nil {
				return err
			}
		}
		if delta.Content != nil && *delta.Content != "" {
			if err := sender(delta.Content, nil); err != nil {
				return err
			}
			receivedAnswer = true
		}
	}
	if err := scanner.Err(); err != nil {
		return fmt.Errorf("moonshot: read stream: %w", err)
	}
	return io.ErrUnexpectedEOF
}

func (m *MoonshotModel) Encode(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([][]float64, error) {
	suffix := m.URLSuffix.Embedding
	if suffix == "" {
		suffix = "embeddings"
	}
	return encodeHTTP(m.httpClient, m.BaseURL, suffix, modelName, texts, apiConfig)
}

func (m *MoonshotModel) ListModels(apiConfig *APIConfig) ([]string, error) {
	if apiConfig == nil || apiConfig.APIKey == nil {
		return nil, fmt.Errorf("API key is nil")
	}
	baseURL, err := resolveModelBaseURL(m.BaseURL, apiConfig.Region)
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequest(http.MethodGet, joinModelURL(baseURL, m.URLSuffix.Models), http.NoBody)
	if err != nil {
		return nil, fmt.Errorf("create model list request: %w", err)
	}
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.APIKey))

	resp, err := m.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("list Moonshot models: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("list Moonshot models: status %d", resp.StatusCode)
	}

	var payload struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err = json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return nil, fmt.Errorf("decode Moonshot model list: %w", err)
	}
	models := make([]string, 0, len(payload.Data))
	for _, model := range payload.Data {
		models = append(models, model.ID)
	}
	return models, nil
}

func (m *MoonshotModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	if apiConfig == nil || apiConfig.APIKey == nil {
		return nil, fmt.Errorf("API key is nil")
	}
	baseURL, err := resolveModelBaseURL(m.BaseURL, apiConfig.Region)
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequest(http.MethodGet, joinModelURL(baseURL, m.URLSuffix.Balance), http.NoBody)
	if err != nil {
		return nil, fmt.Errorf("create balance request: %w", err)
	}
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.APIKey))

	resp, err := m.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("query Moonshot balance: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("query Moonshot balance: status %d", resp.StatusCode)
	}

	var payload struct {
		Data struct {
			AvailableBalance *float64 `json:"available_balance"`
		} `json:"data"`
	}
	if err = json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return nil, fmt.Errorf("decode Moonshot balance: %w", err)
	}
	if payload.Data.AvailableBalance == nil {
		return nil, fmt.Errorf("no balance in response")
	}
	return map[string]interface{}{
		"balance":  *payload.Data.AvailableBalance,
		"currency": "CNY",
	}, nil
}

func (m *MoonshotModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := m.ListModels(apiConfig)
	return err
}

func (m *MoonshotModel) Rerank(modelName *string, query string, texts []string, apiConfig *APIConfig) ([]float64, error) {
	return nil, fmt.Errorf("%s: rerank is not supported", m.Name())
}
