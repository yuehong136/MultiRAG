package models

import (
	"encoding/json"
	"fmt"
	"net/http"
	"time"
)

// MoonshotModel implements provider model discovery for Moonshot.
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

func (m *MoonshotModel) Chat(modelName, message *string, apiConfig *APIConfig, modelConfig *ChatConfig) (*ChatResponse, error) {
	return nil, fmt.Errorf("not implemented")
}

func (m *MoonshotModel) ChatStreamly(modelName, apiKey, message *string, genConf map[string]interface{}) (<-chan string, error) {
	return nil, fmt.Errorf("not implemented")
}

func (m *MoonshotModel) ChatStreamlyWithChannel(modelName, apiKey, message *string, genConf map[string]interface{}, resultChan chan<- string) error {
	return fmt.Errorf("not implemented")
}

func (m *MoonshotModel) ChatStreamlyWithSender(modelName, message *string, apiConfig *APIConfig, modelConfig *ChatConfig, sender func(*string, *string) error) error {
	return fmt.Errorf("not implemented")
}

func (m *MoonshotModel) EncodeToEmbedding(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([][]float64, error) {
	return nil, fmt.Errorf("not implemented")
}

func (m *MoonshotModel) ListModels(apiConfig *APIConfig) ([]string, error) {
	if apiConfig == nil || apiConfig.APIKey == nil {
		return nil, fmt.Errorf("API key is nil")
	}
	baseURL, err := resolveModelBaseURL(m.BaseURL, apiConfig.Region)
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequest(http.MethodPost, joinModelURL(baseURL, m.URLSuffix.Models), http.NoBody)
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
		Models []string `json:"models"`
	}
	if err = json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return nil, fmt.Errorf("decode Moonshot model list: %w", err)
	}
	if len(payload.Models) == 0 {
		return nil, fmt.Errorf("no models in response")
	}
	return payload.Models, nil
}
