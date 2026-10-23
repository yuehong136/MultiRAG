package models

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"strings"
)

// CoHereModel adapts Cohere V2 chat, embeddings and rerank to ModelDriver.
type CoHereModel struct{ *openAIChatDriver }

func NewCoHereModel(baseURL map[string]string, suffix URLSuffix) *CoHereModel {
	return &CoHereModel{newOpenAIChatDriver(baseURL, suffix, "cohere", cohereChatBody)}
}

func cohereChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	body, err := openAIChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	delete(body, "top_p")
	delete(body, "stop")
	if config == nil || config.Temperature == nil {
		body["temperature"] = 0.3
	}
	if config != nil {
		if config.TopP != nil {
			body["p"] = *config.TopP
		}
		if config.Stop != nil {
			body["stop_sequences"] = *config.Stop
		}
		if config.Thinking != nil {
			mode := "disabled"
			if *config.Thinking {
				mode = "enabled"
			}
			body["thinking"] = map[string]string{"type": mode}
		}
	}
	return body, nil
}

// decodeCoHereJSON rejects trailing JSON as well as malformed response bodies.
func decodeCoHereJSON(reader io.Reader, target any) error {
	decoder := json.NewDecoder(reader)
	if err := decoder.Decode(target); err != nil {
		return err
	}
	var trailing any
	if decoder.Decode(&trailing) != io.EOF {
		return fmt.Errorf("provider response has trailing content")
	}
	return nil
}

func (c *CoHereModel) ChatWithMessages(name string, api *APIConfig, messages []Message, config *ChatConfig) (*ChatResponse, error) {
	body, err := cohereChatBody(name, messages, config, false)
	if err != nil {
		return nil, err
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := c.request(http.MethodPost, c.URLSuffix.Chat, local, body)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result struct {
		Message struct {
			Content []struct {
				Type     string `json:"type"`
				Text     string `json:"text"`
				Thinking string `json:"thinking"`
			} `json:"content"`
		} `json:"message"`
		Error        json.RawMessage `json:"error"`
		FinishReason string          `json:"finish_reason"`
	}
	if err := decodeCoHereJSON(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("cohere: decode chat: %w", err)
	}
	if strings.HasPrefix(strings.ToUpper(result.FinishReason), "ERROR") || len(result.Error) > 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("cohere: provider chat error")
	}
	var answer, reasoning strings.Builder
	for _, block := range result.Message.Content {
		if block.Type == "thinking" {
			reasoning.WriteString(block.Thinking)
		} else if block.Type == "text" {
			answer.WriteString(block.Text)
		}
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	if strings.TrimSpace(answer.String()) == "" {
		return nil, fmt.Errorf("cohere: no text answer")
	}
	a, r := answer.String(), reasoning.String()
	return &ChatResponse{Answer: &a, ReasoningContent: &r}, nil
}

func (c *CoHereModel) ChatStreamlyWithSender(name, message *string, api *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if name == nil || message == nil {
		return fmt.Errorf("cohere: model name and message are required")
	}
	return c.ChatStreamlyWithMessages(*name, []Message{{Role: "user", Content: *message}}, api, config, sender)
}

func (c *CoHereModel) ChatStreamlyWithMessages(name string, messages []Message, api *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("cohere: stream sender is required")
	}
	body, err := cohereChatBody(name, messages, config, true)
	if err != nil {
		return err
	}
	resp, err := c.request(http.MethodPost, c.URLSuffix.Chat, api, body)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	receivedAnswer := false
	for scanner.Scan() {
		if err := requestContext(api).Err(); err != nil {
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
		var event struct {
			Type  string `json:"type"`
			Delta struct {
				Message struct {
					Content struct {
						Text     string `json:"text"`
						Thinking string `json:"thinking"`
					} `json:"content"`
				} `json:"message"`
				FinishReason string `json:"finish_reason"`
			} `json:"delta"`
			Error json.RawMessage `json:"error"`
		}
		if err := json.Unmarshal([]byte(data), &event); err != nil {
			return fmt.Errorf("cohere: decode stream: %w", err)
		}
		if event.Type == "error" || len(event.Error) > 0 && string(event.Error) != "null" {
			return fmt.Errorf("cohere: provider stream error")
		}
		switch event.Type {
		case "message-end":
			if !receivedAnswer || strings.HasPrefix(strings.ToUpper(event.Delta.FinishReason), "ERROR") {
				return fmt.Errorf("cohere: stream returned no complete text answer")
			}
			done := "[DONE]"
			return sender(&done, nil)
		case "content-delta":
			content := event.Delta.Message.Content
			if content.Thinking != "" {
				if err := sender(nil, &content.Thinking); err != nil {
					return err
				}
			}
			if err := requestContext(api).Err(); err != nil {
				return err
			}
			if content.Text != "" {
				if err := sender(&content.Text, nil); err != nil {
					return err
				}
				receivedAnswer = receivedAnswer || strings.TrimSpace(content.Text) != ""
			}
		case "message-start", "content-start", "content-end", "citation-start", "citation-end":
			// Metadata events do not contain answer text.
		default:
			return fmt.Errorf("cohere: unsupported stream event %q", event.Type)
		}
	}
	if err := requestContext(api).Err(); err != nil {
		return err
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	return io.ErrUnexpectedEOF
}

func (c *CoHereModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	if len(texts) == 0 {
		return []EmbeddingData{}, nil
	}
	if name == nil || strings.TrimSpace(*name) == "" {
		return nil, fmt.Errorf("cohere: embedding model name is required")
	}
	dimension := 0
	if config != nil {
		dimension = config.Dimension
	}
	if dimension < 0 {
		return nil, fmt.Errorf("cohere: embedding dimension must be non-negative")
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	vectors := make([][]float64, 0, len(texts))
	for start := 0; start < len(texts); start += 32 {
		end := min(start+32, len(texts))
		body := map[string]any{"model": *name, "texts": texts[start:end], "input_type": "search_document", "embedding_types": []string{"float"}}
		if config != nil && config.Query {
			body["input_type"] = "search_query"
		}
		if config != nil && config.Dimension > 0 {
			body["output_dimension"] = config.Dimension
		}
		resp, err := c.request(http.MethodPost, c.URLSuffix.Embedding, local, body)
		if err != nil {
			return nil, err
		}
		var result struct {
			Embeddings struct {
				Float [][]*float64 `json:"float"`
			} `json:"embeddings"`
			Error json.RawMessage `json:"error"`
		}
		decodeErr := decodeCoHereJSON(resp.Body, &result)
		resp.Body.Close()
		if decodeErr != nil {
			return nil, fmt.Errorf("cohere: decode embeddings: %w", decodeErr)
		}
		if len(result.Error) > 0 && string(result.Error) != "null" || len(result.Embeddings.Float) != end-start {
			return nil, fmt.Errorf("cohere: embedding count mismatch or provider error")
		}
		for _, raw := range result.Embeddings.Float {
			if len(raw) == 0 {
				return nil, fmt.Errorf("cohere: empty embedding")
			}
			if dimension == 0 {
				dimension = len(raw)
			}
			if len(raw) != dimension {
				return nil, fmt.Errorf("cohere: inconsistent embedding dimensions")
			}
			vector := make([]float64, len(raw))
			for i, value := range raw {
				if value == nil || math.IsInf(*value, 0) || math.IsNaN(*value) {
					return nil, fmt.Errorf("cohere: invalid embedding value")
				}
				vector[i] = *value
			}
			vectors = append(vectors, vector)
		}
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return embeddingData(vectors), nil
}

func (c *CoHereModel) Rerank(name *string, query string, texts []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	return rerankHTTP(c.httpClient, c.BaseURL, c.URLSuffix.Rerank, name, query, texts, api, config, nil)
}

func (c *CoHereModel) ListModels(api *APIConfig) ([]string, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	names := []string{}
	seenTokens := map[string]bool{}
	token := ""
	for {
		suffix := c.URLSuffix.Models
		if token != "" {
			separator := "?"
			if strings.Contains(suffix, "?") {
				separator = "&"
			}
			suffix += separator + "page_token=" + url.QueryEscape(token)
		}
		resp, err := c.request(http.MethodGet, suffix, local, nil)
		if err != nil {
			return nil, err
		}
		var result struct {
			Models *[]struct {
				Name string `json:"name"`
			} `json:"models"`
			Next  string          `json:"next_page_token"`
			Error json.RawMessage `json:"error"`
		}
		decodeErr := decodeCoHereJSON(resp.Body, &result)
		resp.Body.Close()
		if decodeErr != nil {
			return nil, decodeErr
		}
		if result.Models == nil || len(result.Error) > 0 && string(result.Error) != "null" {
			return nil, fmt.Errorf("cohere: missing model list or provider error")
		}
		for _, item := range *result.Models {
			if strings.TrimSpace(item.Name) == "" {
				return nil, fmt.Errorf("cohere: invalid model name")
			}
			names = append(names, item.Name)
		}
		if err := requestContext(local).Err(); err != nil {
			return nil, err
		}
		if result.Next == "" {
			return names, nil
		}
		if seenTokens[result.Next] {
			return nil, fmt.Errorf("cohere: repeated model page token")
		}
		seenTokens[result.Next] = true
		token = result.Next
	}
}

func (c *CoHereModel) CheckConnection(api *APIConfig) error {
	_, err := c.ListModels(api)
	return err
}

var _ ModelDriver = (*CoHereModel)(nil)
