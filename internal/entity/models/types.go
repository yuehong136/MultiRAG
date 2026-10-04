package models

import "context"

// Message carries text or OpenAI-style text/image_url content parts.
type Message struct {
	Role             string  `json:"role"`
	Content          any     `json:"content"`
	ReasoningContent *string `json:"reasoning_content,omitempty"`
}

// EmbeddingModel interface for embedding models
type ModelDriver interface {
	Name() string

	// ChatWithMessages sends multiple role-tagged messages and returns a response.
	ChatWithMessages(modelName string, apiConfig *APIConfig, messages []Message, modelConfig *ChatConfig) (*ChatResponse, error)
	// ChatStreamly sends a message and streams response
	ChatStreamly(modelName, apiKey, message *string, genConf map[string]interface{}) (<-chan string, error)
	// ChatStreamlyWithChannel sends a message and streams response to channel (better performance)
	ChatStreamlyWithChannel(modelName, apiKey, message *string, genConf map[string]interface{}, resultChan chan<- string) error
	// ChatStreamlyWithSender sends a message and streams response via sender function (best performance, no channel)
	ChatStreamlyWithSender(modelName, message *string, apiConfig *APIConfig, modelConfig *ChatConfig, sender func(*string, *string) error) error
	// ChatStreamlyWithMessages preserves roles and request cancellation while streaming.
	ChatStreamlyWithMessages(modelName string, messages []Message, apiConfig *APIConfig, modelConfig *ChatConfig, sender func(*string, *string) error) error
	// Encode encodes a list of texts into embeddings with the selected configuration.
	Encode(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([][]float64, error)
	Rerank(modelName *string, query string, texts []string, apiConfig *APIConfig) ([]float64, error)
	// ListModels lists models supported by the configured provider instance.
	ListModels(apiConfig *APIConfig) ([]string, error)

	Balance(apiConfig *APIConfig) (map[string]interface{}, error)

	CheckConnection(apiConfig *APIConfig) error
}

type ChatResponse struct {
	Answer           *string `json:"answer"`
	ReasoningContent *string `json:"reasoning_content"`
}

// URLSuffix represents the URL suffixes for different API endpoints
type URLSuffix struct {
	Chat        string `json:"chat"`
	AsyncChat   string `json:"async_chat"`
	AsyncResult string `json:"async_result"`
	Embedding   string `json:"embedding"`
	Rerank      string `json:"rerank"`
	Models      string `json:"models"`
	Balance     string `json:"balance"`
	Files       string `json:"files"`
	Status      string `json:"status"`
}

type ChatConfig struct {
	Vision      *bool
	Stream      *bool
	Thinking    *bool
	MaxTokens   *int
	Temperature *float64
	TopP        *float64
	DoSample    *bool
	Stop        *[]string
	ModelClass  *string // Model family for provider behavior, not a capability such as chat.
	Effort      *string
	Verbosity   *string
}

type APIConfig struct {
	// Context carries request cancellation; nil uses a bounded background context.
	Context context.Context
	APIKey  *string
	Region  *string
}

type EmbeddingConfig struct{}
