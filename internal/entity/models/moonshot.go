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

func (m *MoonshotModel) Name() string {
	return "moonshot"
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
