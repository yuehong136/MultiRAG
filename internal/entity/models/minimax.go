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

// MinimaxModel implements Minimax text chat and reasoning streams with request cancellation.
type MinimaxModel struct {
	BaseURL    map[string]string
	URLSuffix  URLSuffix
	httpClient *http.Client
}

func NewMinimaxModel(baseURL map[string]string, urlSuffix URLSuffix) *MinimaxModel {
	return &MinimaxModel{
		BaseURL:    baseURL,
		URLSuffix:  urlSuffix,
		httpClient: &http.Client{Timeout: 120 * time.Second},
	}
}

func (m *MinimaxModel) Name() string {
	return "minimax"
}

func (m *MinimaxModel) Chat(modelName, message *string, apiConfig *APIConfig, modelConfig *ChatConfig) (*ChatResponse, error) {
	if modelName == nil || message == nil {
		return nil, fmt.Errorf("minimax: model name and message are required")
	}
	return m.chat(*modelName, []Message{{Role: "user", Content: *message}}, apiConfig, modelConfig)
}

func (m *MinimaxModel) ChatWithMessages(modelName string, apiConfig *APIConfig, messages []Message, modelConfig *ChatConfig) (*ChatResponse, error) {
	return m.chat(modelName, messages, apiConfig, modelConfig)
}

func (m *MinimaxModel) ChatStreamly(modelName, apiKey, message *string, genConf map[string]interface{}) (<-chan string, error) {
	return nil, fmt.Errorf("minimax: channel-only streaming is unsupported; use the sender interface")
}

func (m *MinimaxModel) ChatStreamlyWithChannel(modelName, apiKey, message *string, genConf map[string]interface{}, resultChan chan<- string) error {
	return fmt.Errorf("minimax: channel-only streaming is unsupported; use the sender interface")
}

func minimaxChatBody(modelName string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	if strings.TrimSpace(modelName) == "" {
		return nil, fmt.Errorf("minimax: model name is required")
	}
	if err := ValidateMessages(messages); err != nil {
		return nil, err
	}
	if stream {
		if err := ValidateTextMessages(messages); err != nil {
			return nil, err
		}
	}
	apiMessages := messages

	// The entry point determines the response protocol, regardless of config.Stream.
	body := map[string]interface{}{"model": modelName, "messages": apiMessages, "stream": stream, "temperature": 1}
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
			mode = "adaptive"
			body["reasoning_split"] = true
		}
		body["thinking"] = map[string]string{"type": mode}
	}
	return body, nil
}

func (m *MinimaxModel) requestChat(body map[string]interface{}, config *APIConfig) (*http.Response, error) {
	if config == nil || config.APIKey == nil || strings.TrimSpace(*config.APIKey) == "" {
		return nil, fmt.Errorf("minimax: API key is required")
	}
	if m.URLSuffix.Chat == "" {
		return nil, fmt.Errorf("minimax: chat endpoint suffix is missing")
	}
	baseURL, err := resolveModelBaseURL(m.BaseURL, config.Region)
	if err != nil {
		return nil, err
	}
	data, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("minimax: encode request: %w", err)
	}
	req, err := http.NewRequestWithContext(requestContext(config), http.MethodPost, joinModelURL(baseURL, m.URLSuffix.Chat), bytes.NewReader(data))
	if err != nil {
		return nil, fmt.Errorf("minimax: create request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*config.APIKey))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	if body["stream"] == true {
		req.Header.Set("Accept", "text/event-stream")
	}
	resp, err := m.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("minimax: send request: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("minimax: request failed (HTTP %d)", resp.StatusCode)
	}
	return resp, nil
}

func (m *MinimaxModel) chat(modelName string, messages []Message, apiConfig *APIConfig, config *ChatConfig) (*ChatResponse, error) {
	body, err := minimaxChatBody(modelName, messages, config, false)
	if err != nil {
		return nil, err
	}
	resp, err := m.requestChat(body, apiConfig)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result struct {
		aliyunChatResponse
		minimaxStatus
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("minimax: decode response: %w", err)
	}
	if err := result.minimaxStatus.err(); err != nil {
		return nil, err
	}
	if len(result.Error) != 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("minimax: provider returned an error")
	}
	if len(result.Choices) == 0 || result.Choices[0].Message.Content == nil || *result.Choices[0].Message.Content == "" {
		return nil, fmt.Errorf("minimax: no text answer")
	}
	message := result.Choices[0].Message
	// Reasoning is optional, including when the provider enables thinking by default.
	if message.ReasoningContent != nil {
		reasoning := strings.TrimPrefix(*message.ReasoningContent, "\n")
		message.ReasoningContent = &reasoning
	}
	return &ChatResponse{Answer: message.Content, ReasoningContent: message.ReasoningContent}, nil
}

func (m *MinimaxModel) ChatStreamlyWithSender(modelName, message *string, apiConfig *APIConfig, modelConfig *ChatConfig, sender func(*string, *string) error) error {
	if modelName == nil || message == nil {
		return fmt.Errorf("model name and message are required")
	}
	return m.ChatStreamlyWithMessages(*modelName, []Message{{Role: "user", Content: *message}}, apiConfig, modelConfig, sender)
}

func (m *MinimaxModel) ChatStreamlyWithMessages(modelName string, messages []Message, apiConfig *APIConfig, modelConfig *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("stream sender is required")
	}
	body, err := minimaxChatBody(modelName, messages, modelConfig, true)
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
	receivedAnswer, finished := false, false
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
				return fmt.Errorf("minimax: stream returned no text answer")
			}
			return sender(&data, nil)
		}
		var event struct {
			minimaxStatus
			Choices []struct {
				Delta struct {
					Content          *string `json:"content"`
					ReasoningContent *string `json:"reasoning_content"`
				} `json:"delta"`
				FinishReason *string `json:"finish_reason"`
			} `json:"choices"`
			Error json.RawMessage `json:"error"`
		}
		if err := json.Unmarshal([]byte(data), &event); err != nil {
			return fmt.Errorf("minimax: decode stream: %w", err)
		}
		if err := event.minimaxStatus.err(); err != nil {
			return err
		}
		if len(event.Error) != 0 && string(event.Error) != "null" {
			return fmt.Errorf("minimax: provider stream error")
		}
		if len(event.Choices) == 0 {
			continue
		}
		if reason := event.Choices[0].FinishReason; reason != nil && *reason != "" {
			finished = true
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
		return fmt.Errorf("minimax: read stream: %w", err)
	}
	// Native MiniMax may terminate after finish_reason without an OpenAI [DONE].
	// Wait for EOF so a later provider/transport error still wins over completion.
	if finished && receivedAnswer {
		if err := requestContext(apiConfig).Err(); err != nil {
			return err
		}
		done := "[DONE]"
		return sender(&done, nil)
	}
	return io.ErrUnexpectedEOF
}

func (m *MinimaxModel) Encode(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([][]float64, error) {
	return nil, fmt.Errorf("embedding is not implemented for %s", m.Name())
}

func (m *MinimaxModel) ListModels(apiConfig *APIConfig) ([]string, error) {
	if apiConfig == nil || apiConfig.APIKey == nil || strings.TrimSpace(*apiConfig.APIKey) == "" {
		return nil, fmt.Errorf("minimax: API key is required")
	}
	if m.URLSuffix.Models == "" {
		return nil, fmt.Errorf("minimax: models endpoint suffix is missing")
	}
	baseURL, err := resolveModelBaseURL(m.BaseURL, apiConfig.Region)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(requestContext(apiConfig), http.MethodGet, joinModelURL(baseURL, m.URLSuffix.Models), http.NoBody)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*apiConfig.APIKey))
	resp, err := m.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("minimax: list models failed (HTTP %d)", resp.StatusCode)
	}
	var result struct {
		minimaxStatus
		Error json.RawMessage `json:"error"`
		Data  *[]struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("minimax: decode model list: %w", err)
	}
	if err := result.minimaxStatus.err(); err != nil {
		return nil, err
	}
	if len(result.Error) != 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("minimax: provider returned an error")
	}
	if result.Data == nil {
		return nil, fmt.Errorf("minimax: missing model list")
	}
	names := make([]string, 0, len(*result.Data))
	for _, model := range *result.Data {
		if strings.TrimSpace(model.ID) == "" {
			return nil, fmt.Errorf("minimax: missing model ID")
		}
		names = append(names, model.ID)
	}
	return names, nil
}

func (m *MinimaxModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("balance query is not implemented for %s", m.Name())
}

func (m *MinimaxModel) CheckConnection(apiConfig *APIConfig) error {
	return checkBearerEndpointConnection(m.httpClient, m.BaseURL, m.URLSuffix.Files, apiConfig, m.Name())
}

func (m *MinimaxModel) Rerank(modelName *string, query string, texts []string, apiConfig *APIConfig) ([]float64, error) {
	return nil, fmt.Errorf("%s: rerank is not supported", m.Name())
}

// MiniMax reports API failures inside successful HTTP responses as well.
// Preserve the numeric code without exposing provider messages or request data.
type minimaxStatus struct {
	BaseResponse *struct {
		StatusCode int `json:"status_code"`
	} `json:"base_resp"`
}

func (s minimaxStatus) err() error {
	if s.BaseResponse != nil && s.BaseResponse.StatusCode != 0 {
		return fmt.Errorf("minimax: provider error (status_code %d)", s.BaseResponse.StatusCode)
	}
	return nil
}
