package models

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"maps"
	"math"
	"net/http"
	"strings"
	"time"
)

// MoonshotModel implements Moonshot text chat, reasoning streams and discovery.
type MoonshotModel struct {
	BaseURL           map[string]string
	URLSuffix         URLSuffix
	httpClient        *http.Client
	streamIdleTimeout time.Duration
}

func NewMoonshotModel(baseURL map[string]string, urlSuffix URLSuffix) *MoonshotModel {
	return &MoonshotModel{
		BaseURL:           maps.Clone(baseURL),
		URLSuffix:         urlSuffix,
		httpClient:        &http.Client{Transport: openAIChatTransport, CheckRedirect: rejectModelRedirect},
		streamIdleTimeout: 60 * time.Second,
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

func (m *MoonshotModel) ChatWithMessages(modelName string, apiConfig *APIConfig, messages []Message, modelConfig *ChatConfig) (*ChatResponse, error) {
	return m.chat(modelName, messages, apiConfig, modelConfig)
}

func (m *MoonshotModel) ChatStreamly(modelName, apiKey, message *string, genConf map[string]interface{}) (<-chan string, error) {
	return nil, fmt.Errorf("moonshot: channel-only streaming is unsupported; use the sender interface")
}

func (m *MoonshotModel) ChatStreamlyWithChannel(modelName, apiKey, message *string, genConf map[string]interface{}, resultChan chan<- string) error {
	return fmt.Errorf("moonshot: channel-only streaming is unsupported; use the sender interface")
}

func moonshotChatBody(modelName string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	modelName = strings.TrimSpace(modelName)
	if modelName == "" {
		return nil, fmt.Errorf("moonshot: model name is required")
	}
	if err := ValidateMessages(messages); err != nil {
		return nil, err
	}
	apiMessages := messages

	// The entry point determines the response protocol, regardless of config.Stream.
	body := map[string]interface{}{"model": modelName, "messages": apiMessages, "stream": stream, "temperature": 0.6}
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
	local, cancel := modelCallConfig(apiConfig)
	defer cancel()
	resp, err := m.requestChat(body, local)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result aliyunChatResponse
	if err := decodeBoundedModelJSON(resp.Body, &result); err != nil {
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
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return &ChatResponse{Answer: message.Content, ReasoningContent: message.ReasoningContent}, nil
}

func (m *MoonshotModel) ChatStreamlyWithSender(modelName, message *string, apiConfig *APIConfig, modelConfig *ChatConfig, sender func(*string, *string) error) error {
	if modelName == nil || message == nil {
		return fmt.Errorf("model name and message are required")
	}
	return m.ChatStreamlyWithMessages(*modelName, []Message{{Role: "user", Content: *message}}, apiConfig, modelConfig, sender)
}

func (m *MoonshotModel) ChatStreamlyWithMessages(modelName string, messages []Message, apiConfig *APIConfig, modelConfig *ChatConfig, sender func(*string, *string) error) (streamErr error) {
	apiConfig, cancelCall := modelStreamConfig(apiConfig)
	defer cancelCall()
	if sender == nil {
		return fmt.Errorf("stream sender is required")
	}
	body, err := moonshotChatBody(modelName, messages, modelConfig, true)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithCancelCause(requestContext(apiConfig))
	local := APIConfig{Context: ctx}
	if apiConfig != nil {
		local = *apiConfig
		local.Context = ctx
	}
	idle := m.streamIdleTimeout
	if idle <= 0 {
		idle = 60 * time.Second
	}
	idleError := fmt.Errorf("moonshot: stream idle timeout: %w", context.DeadlineExceeded)
	timer := time.AfterFunc(idle, func() { cancel(idleError) })
	defer func() {
		timer.Stop()
		if context.Cause(ctx) == idleError {
			streamErr = idleError
		}
		cancel(nil)
	}()
	resp, err := m.requestChat(body, &local)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	resp.Body = &activeModelStream{ReadCloser: resp.Body, active: func() { timer.Reset(idle) }}
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	receivedAnswer := false
	for scanner.Scan() {
		if err := ctx.Err(); err != nil {
			return err
		}
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
		if err := ctx.Err(); err != nil {
			return err
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

func (m *MoonshotModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	suffix := m.URLSuffix.Embedding
	if suffix == "" {
		suffix = "embeddings"
	}
	return embedHTTPWithConfig(m.httpClient, m.BaseURL, suffix, modelName, texts, apiConfig, embeddingConfig)
}

func (m *MoonshotModel) get(suffix string, api *APIConfig) (*http.Response, error) {
	if api == nil || api.APIKey == nil || strings.TrimSpace(*api.APIKey) == "" {
		return nil, fmt.Errorf("moonshot: API key required")
	}
	if suffix == "" {
		return nil, fmt.Errorf("moonshot: endpoint missing")
	}
	base, err := resolveModelBaseURL(m.BaseURL, api.Region)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(requestContext(api), http.MethodGet, joinModelURL(base, suffix), nil)
	if err != nil {
		return nil, fmt.Errorf("moonshot: invalid endpoint")
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*api.APIKey))
	req.Header.Set("Accept", "application/json")
	resp, err := m.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("moonshot: GET failed: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("moonshot: GET failed (HTTP %d)", resp.StatusCode)
	}
	return resp, nil
}

func (m *MoonshotModel) ListModels(api *APIConfig) ([]string, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := m.get(m.URLSuffix.Models, local)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	names, err := strictProviderModelNames(resp.Body)
	if err == nil {
		err = requestContext(local).Err()
	}
	return names, err
}

func (m *MoonshotModel) Balance(api *APIConfig) (map[string]interface{}, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := m.get(m.URLSuffix.Balance, local)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var payload struct {
		Data struct {
			AvailableBalance *float64 `json:"available_balance"`
		} `json:"data"`
		Error json.RawMessage `json:"error"`
	}
	if err := decodeBoundedModelJSON(resp.Body, &payload); err != nil || payload.Data.AvailableBalance == nil || len(payload.Error) > 0 && string(payload.Error) != "null" {
		return nil, fmt.Errorf("moonshot: invalid balance response")
	}
	amount := *payload.Data.AvailableBalance
	if math.IsNaN(amount) || math.IsInf(amount, 0) {
		return nil, fmt.Errorf("moonshot: invalid balance amount")
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return map[string]interface{}{"balance": amount, "currency": "CNY"}, nil
}

func (m *MoonshotModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := m.ListModels(apiConfig)
	return err
}

func (m *MoonshotModel) Rerank(modelName *string, query string, texts []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return nil, fmt.Errorf("%s: rerank is not supported", m.Name())
}
