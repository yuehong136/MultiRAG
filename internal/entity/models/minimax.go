package models

import (
	"fmt"
	"net/http"
	"time"
)

// MinimaxModel implements the MiniMax provider connection check introduced
// before the provider's chat and TTS methods were implemented upstream.
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
	return nil, fmt.Errorf("chat is not implemented for %s", m.Name())
}

func (m *MinimaxModel) ChatWithMessages(modelName string, apiKey *string, messages []Message, modelConfig *ChatConfig) (string, error) {
	return "", fmt.Errorf("%s, ChatWithMessages not implemented", m.Name())
}

func (m *MinimaxModel) ChatStreamly(modelName, apiKey, message *string, genConf map[string]interface{}) (<-chan string, error) {
	return nil, fmt.Errorf("streaming chat is not implemented for %s", m.Name())
}

func (m *MinimaxModel) ChatStreamlyWithChannel(modelName, apiKey, message *string, genConf map[string]interface{}, resultChan chan<- string) error {
	return fmt.Errorf("streaming chat is not implemented for %s", m.Name())
}

func (m *MinimaxModel) ChatStreamlyWithSender(modelName, message *string, apiConfig *APIConfig, modelConfig *ChatConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("streaming chat is not implemented for %s", m.Name())
}

func (m *MinimaxModel) EncodeToEmbedding(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([][]float64, error) {
	return nil, fmt.Errorf("embedding is not implemented for %s", m.Name())
}

func (m *MinimaxModel) ListModels(apiConfig *APIConfig) ([]string, error) {
	return nil, fmt.Errorf("model discovery is not implemented for %s", m.Name())
}

func (m *MinimaxModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("balance query is not implemented for %s", m.Name())
}

func (m *MinimaxModel) CheckConnection(apiConfig *APIConfig) error {
	return checkBearerEndpointConnection(m.httpClient, m.BaseURL, m.URLSuffix.Files, apiConfig, m.Name())
}
