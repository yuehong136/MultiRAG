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
		Thinking    *bool     `json:"thinking"`
		MaxTokens   *int      `json:"max_tokens"`
		Temperature *float64  `json:"temperature"`
		TopP        *float64  `json:"top_p"`
		DoSample    *bool     `json:"do_sample"`
		Stop        *[]string `json:"stop"`
		Effort      *string   `json:"reasoning_effort"`
	}
	if err := json.Unmarshal(data, &parsed); err != nil {
		return nil, fmt.Errorf("invalid generation config: %w", err)
	}
	result := defaults
	if parsed.Thinking != nil {
		result.Thinking = parsed.Thinking
	}
	result.MaxTokens, result.Temperature, result.TopP = parsed.MaxTokens, parsed.Temperature, parsed.TopP
	result.DoSample, result.Stop, result.Effort = parsed.DoSample, parsed.Stop, parsed.Effort
	return &result, nil
}

func (m *ChatModel) Chat(system string, history []map[string]string, values map[string]interface{}) (string, error) {
	config, err := chatGenerationConfig(values, m.ModelConfig)
	if err != nil {
		return "", err
	}
	messages := make([]Message, 0, len(history)+1)
	if system != "" {
		messages = append(messages, Message{Role: "system", Content: system})
	}
	for _, item := range history {
		if item["role"] != "" && item["content"] != "" {
			messages = append(messages, Message{Role: item["role"], Content: item["content"]})
		}
	}
	return m.ModelDriver.ChatWithMessages(*m.ModelName, m.APIConfig.APIKey, messages, config)
}

// ChatSession's existing channel API has no error channel. Preserve its complete
// history and surface request failure before returning a channel.
func (m *ChatModel) ChatStreamly(system string, history []map[string]string, values map[string]interface{}) (<-chan string, error) {
	answer, err := m.Chat(system, history, values)
	if err != nil {
		return nil, err
	}
	result := make(chan string, 1)
	result <- answer
	close(result)
	return result, nil
}
