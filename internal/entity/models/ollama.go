package models

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// OllamaModel uses the native chat protocol, including NDJSON streaming.
type OllamaModel struct {
	*DummyModel
	httpClient *http.Client
}

func NewOllamaModel(baseURL map[string]string, suffix URLSuffix) *OllamaModel {
	return &OllamaModel{DummyModel: NewDummyModel(baseURL, suffix), httpClient: &http.Client{}}
}
func (o *OllamaModel) Name() string { return "ollama" }

func ollamaBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	if strings.TrimSpace(name) == "" {
		return nil, fmt.Errorf("ollama: model name is required")
	}
	if err := ValidateMessages(messages); err != nil {
		return nil, err
	}
	history := make([]map[string]any, 0, len(messages))
	for _, message := range messages {
		role := message.Role
		if role == "model" {
			role = "assistant"
		}
		item := map[string]any{"role": role}
		if message.ReasoningContent != nil {
			item["thinking"] = *message.ReasoningContent
		}
		if text, ok := message.Content.(string); ok {
			item["content"] = text
		} else {
			parts, err := ContentParts(message.Content)
			if err != nil {
				return nil, err
			}
			var texts, images []string
			for _, part := range parts {
				switch part["type"] {
				case "text":
					texts = append(texts, part["text"].(string))
				case "image_url":
					raw := part["image_url"].(map[string]any)["url"].(string)
					_, data, err := imageData(raw)
					if err != nil {
						return nil, fmt.Errorf("ollama: images require base64 data URLs; remote image fetching is not supported")
					}
					images = append(images, base64.StdEncoding.EncodeToString(data))
				default:
					return nil, fmt.Errorf("ollama: unsupported content part %s", part["type"])
				}
			}
			item["content"] = strings.Join(texts, "\n")
			if len(images) > 0 {
				item["images"] = images
			}
		}
		history = append(history, item)
	}
	body := map[string]any{"model": name, "messages": history, "stream": stream}
	options := map[string]any{}
	if config != nil {
		if config.MaxTokens != nil {
			options["num_predict"] = *config.MaxTokens
		}
		if config.Temperature != nil {
			options["temperature"] = *config.Temperature
		}
		if config.TopP != nil {
			options["top_p"] = *config.TopP
		}
		if config.Stop != nil {
			options["stop"] = *config.Stop
		}
		if config.Thinking != nil {
			body["think"] = *config.Thinking
		}
		if config.Effort != nil && *config.Effort != "" && *config.Effort != "default" && (config.Thinking == nil || *config.Thinking) {
			body["think"] = *config.Effort
		}
	}
	if len(options) > 0 {
		body["options"] = options
	}
	return body, nil
}
func (o *OllamaModel) request(method, suffix string, api *APIConfig, body any) (*http.Response, error) {
	var region *string
	if api != nil {
		region = api.Region
	}
	base, err := resolveModelBaseURL(o.BaseURL, region)
	if err != nil {
		return nil, err
	}
	if suffix == "" {
		return nil, fmt.Errorf("ollama: endpoint suffix is missing")
	}
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(requestContext(api), method, joinModelURL(base, suffix), reader)
	if err != nil {
		return nil, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if api != nil && api.APIKey != nil && *api.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+*api.APIKey)
	}
	response, err := o.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("ollama request: %w", err)
	}
	if response.StatusCode != http.StatusOK {
		response.Body.Close()
		return nil, fmt.Errorf("ollama: HTTP %d", response.StatusCode)
	}
	return response, nil
}

type ollamaResponse struct {
	Message struct {
		Content  *string `json:"content"`
		Thinking *string `json:"thinking"`
	} `json:"message"`
	Done  bool            `json:"done"`
	Error json.RawMessage `json:"error"`
}

func (o *OllamaModel) ChatWithMessages(name string, api *APIConfig, messages []Message, config *ChatConfig) (*ChatResponse, error) {
	body, err := ollamaBody(name, messages, config, false)
	if err != nil {
		return nil, err
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	response, err := o.request(http.MethodPost, o.URLSuffix.Chat, local, body)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	var payload ollamaResponse
	if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
		return nil, err
	}
	if len(payload.Error) > 0 && string(payload.Error) != "null" {
		return nil, fmt.Errorf("ollama: provider error")
	}
	result := &ChatResponse{Answer: payload.Message.Content, ReasoningContent: payload.Message.Thinking}
	if !payload.Done {
		return nil, io.ErrUnexpectedEOF
	}
	if err := ValidateChatResponse(result); err != nil {
		return nil, err
	}
	return result, nil
}
func (o *OllamaModel) ChatStreamlyWithSender(name, message *string, api *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if name == nil || message == nil {
		return fmt.Errorf("ollama: model and message are required")
	}
	return o.ChatStreamlyWithMessages(*name, []Message{{Role: "user", Content: *message}}, api, config, sender)
}
func (o *OllamaModel) ChatStreamlyWithMessages(name string, messages []Message, api *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("ollama: sender is required")
	}
	body, err := ollamaBody(name, messages, config, true)
	if err != nil {
		return err
	}
	response, err := o.request(http.MethodPost, o.URLSuffix.Chat, api, body)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	decoder := json.NewDecoder(response.Body)
	received := false
	for {
		var payload ollamaResponse
		if err := decoder.Decode(&payload); err != nil {
			if err == io.EOF {
				return io.ErrUnexpectedEOF
			}
			return err
		}
		if len(payload.Error) > 0 && string(payload.Error) != "null" {
			return fmt.Errorf("ollama: provider stream error")
		}
		if r := payload.Message.Thinking; r != nil && *r != "" {
			if err := sender(nil, r); err != nil {
				return err
			}
		}
		if c := payload.Message.Content; c != nil && *c != "" {
			received = true
			if err := sender(c, nil); err != nil {
				return err
			}
		}
		if payload.Done {
			if !received {
				return fmt.Errorf("ollama: stream returned no answer")
			}
			if err := requestContext(api).Err(); err != nil {
				return err
			}
			done := "[DONE]"
			return sender(&done, nil)
		}
	}
}
func (o *OllamaModel) ListModels(api *APIConfig) ([]string, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	response, err := o.request(http.MethodGet, o.URLSuffix.Models, local, nil)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	var payload struct {
		Models *[]struct {
			Name string `json:"name"`
		} `json:"models"`
		Error json.RawMessage `json:"error"`
	}
	if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
		return nil, err
	}
	if payload.Models == nil || (len(payload.Error) > 0 && string(payload.Error) != "null") {
		return nil, fmt.Errorf("ollama: invalid model list")
	}
	names := make([]string, 0, len(*payload.Models))
	for _, model := range *payload.Models {
		if model.Name == "" {
			return nil, fmt.Errorf("ollama: missing model name")
		}
		names = append(names, model.Name)
	}
	return names, nil
}
func (o *OllamaModel) CheckConnection(api *APIConfig) error { _, err := o.ListModels(api); return err }

// modelCallConfig bounds non-streaming calls without imposing a total SSE timeout.
func modelCallConfig(api *APIConfig) (*APIConfig, context.CancelFunc) {
	local := APIConfig{}
	if api != nil {
		local = *api
	}
	ctx, cancel := context.WithTimeout(requestContext(api), 120*time.Second)
	local.Context = ctx
	return &local, cancel
}
