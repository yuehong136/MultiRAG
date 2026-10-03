package models

import (
	"encoding/json"
	"fmt"
)

// Bound models hold a request-scoped driver, resolved name and credentials.
// Retrieval and ChatSession use the same provider implementation as the API.
type EmbeddingModel struct {
	ModelDriver     ModelDriver
	ModelName       *string
	APIConfig       *APIConfig
	EmbeddingConfig *EmbeddingConfig
}

func NewEmbeddingModel(driver ModelDriver, name *string, config *APIConfig) *EmbeddingModel {
	return &EmbeddingModel{ModelDriver: driver, ModelName: name, APIConfig: config}
}
func (m *EmbeddingModel) Encode(texts []string) ([][]float64, error) {
	embeddings, err := m.ModelDriver.Encode(m.ModelName, texts, m.APIConfig, m.EmbeddingConfig)
	if err != nil {
		return nil, err
	}
	if err := ValidateEmbeddings(embeddings, len(texts)); err != nil {
		return nil, err
	}
	return embeddings, nil
}

type RerankModel struct {
	ModelDriver ModelDriver
	ModelName   *string
	APIConfig   *APIConfig
}

func NewRerankModel(driver ModelDriver, name *string, config *APIConfig) *RerankModel {
	return &RerankModel{driver, name, config}
}
func (m *RerankModel) Rerank(query string, texts []string) ([]float64, error) {
	return m.ModelDriver.Rerank(m.ModelName, query, texts, m.APIConfig)
}

type ChatModel struct {
	ModelDriver ModelDriver
	ModelName   *string
	APIConfig   *APIConfig
	ModelConfig ChatConfig
}

func NewChatModel(driver ModelDriver, name *string, config *APIConfig) *ChatModel {
	return &ChatModel{ModelDriver: driver, ModelName: name, APIConfig: config}
}

func chatGenerationConfig(values map[string]interface{}, defaults ChatConfig) (*ChatConfig, error) {
	// Map the established ChatSession keys to the driver's typed configuration.
	data, err := json.Marshal(values)
	if err != nil {
		return nil, err
	}
	var parsed struct {
		Stream      *bool     `json:"stream"`
		Thinking    *bool     `json:"thinking"`
		MaxTokens   *int      `json:"max_tokens"`
		Temperature *float64  `json:"temperature"`
		TopP        *float64  `json:"top_p"`
		DoSample    *bool     `json:"do_sample"`
		Stop        *[]string `json:"stop"`
		Effort      *string   `json:"reasoning_effort"`
		EffortAlias *string   `json:"effort"`
		Verbosity   *string   `json:"verbosity"`
	}
	if err := json.Unmarshal(data, &parsed); err != nil {
		return nil, fmt.Errorf("invalid generation config: %w", err)
	}
	result := defaults
	if parsed.Stream != nil {
		result.Stream = parsed.Stream
	}
	if parsed.Thinking != nil {
		result.Thinking = parsed.Thinking
	}
	if parsed.MaxTokens != nil {
		result.MaxTokens = parsed.MaxTokens
	}
	if parsed.Temperature != nil {
		result.Temperature = parsed.Temperature
	}
	if parsed.TopP != nil {
		result.TopP = parsed.TopP
	}
	if parsed.DoSample != nil {
		result.DoSample = parsed.DoSample
	}
	if parsed.Stop != nil {
		result.Stop = parsed.Stop
	}
	if parsed.Effort != nil {
		result.Effort = parsed.Effort
	} else if parsed.EffortAlias != nil {
		result.Effort = parsed.EffortAlias
	}
	if parsed.Verbosity != nil {
		result.Verbosity = parsed.Verbosity
	}
	if result.Thinking != nil && !*result.Thinking {
		result.Effort, result.Verbosity = nil, nil
	}

	return &result, nil
}

func (m *ChatModel) Chat(system string, history []map[string]string, values map[string]interface{}) (string, error) {
	config, err := chatGenerationConfig(values, m.ModelConfig)
	if err != nil {
		return "", err
	}
	if err := requestContext(m.APIConfig).Err(); err != nil {
		return "", err
	}
	answer, err := m.ModelDriver.ChatWithMessages(*m.ModelName, m.APIConfig, chatHistory(system, history), config)
	if err != nil {
		return "", err
	}
	if err := requestContext(m.APIConfig).Err(); err != nil {
		return "", err
	}
	return answer, nil
}

func chatHistory(system string, history []map[string]string) []Message {
	messages := make([]Message, 0, len(history)+1)
	if system != "" {
		messages = append(messages, Message{Role: "system", Content: system})
	}
	for _, item := range history {
		if item["role"] != "" && item["content"] != "" {
			messages = append(messages, Message{Role: item["role"], Content: item["content"]})
		}
	}
	return messages
}

func (m *ChatModel) ChatStreamlyWithSender(system string, history []map[string]string, values map[string]interface{}, sender func(*string, *string) error) error {
	config, err := chatGenerationConfig(values, m.ModelConfig)
	if err != nil {
		return err
	}
	if sender == nil {
		return fmt.Errorf("chat sender is required")
	}
	completed, receivedAnswer := false, false
	guardedSender := func(content, reasoning *string) error {
		if err := requestContext(m.APIConfig).Err(); err != nil {
			return err
		}
		if completed {
			return fmt.Errorf("chat stream continued after completion")
		}
		if content != nil && *content == "[DONE]" {
			if !receivedAnswer {
				return fmt.Errorf("chat stream returned no text answer")
			}
			completed = true
			return nil
		}
		if content != nil && *content != "" {
			receivedAnswer = true
		}
		return sender(content, reasoning)
	}
	if err := m.ModelDriver.ChatStreamlyWithMessages(*m.ModelName, chatHistory(system, history), m.APIConfig, config, guardedSender); err != nil {
		return err
	}
	if err := requestContext(m.APIConfig).Err(); err != nil {
		return err
	}
	if !completed {
		return fmt.Errorf("chat stream ended before completion")
	}
	done := "[DONE]"
	return sender(&done, nil)
}
