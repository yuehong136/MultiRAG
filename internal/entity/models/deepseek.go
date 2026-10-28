package models

import (
	"bytes"
	"encoding/json"
	"fmt"
	"maps"
	"net/http"
	"strings"
)

// DeepSeekModel implements provider model discovery for DeepSeek.
type DeepSeekModel struct {
	BaseURL    map[string]string
	URLSuffix  URLSuffix
	httpClient *http.Client
}

func NewDeepSeekModel(baseURL map[string]string, urlSuffix URLSuffix) *DeepSeekModel {
	return &DeepSeekModel{
		BaseURL:    maps.Clone(baseURL),
		URLSuffix:  urlSuffix,
		httpClient: &http.Client{Transport: openAIChatTransport},
	}
}

func (m *DeepSeekModel) Name() string {
	return "deepseek"
}

func (m *DeepSeekModel) Chat(modelName, message *string, apiConfig *APIConfig, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	if modelName == nil || message == nil {
		return nil, fmt.Errorf("model and message are required")
	}
	return m.ChatWithMessages(*modelName, apiConfig, []Message{{Role: "user", Content: *message}}, chatModelConfig)
}
func (m *DeepSeekModel) ChatWithMessages(modelName string, apiConfig *APIConfig, messages []Message, chatModelConfig *ChatConfig) (*ChatResponse, error) {
	apiConfig, cancelCall := modelCallConfig(apiConfig)
	defer cancelCall()
	if strings.TrimSpace(modelName) == "" {
		return nil, fmt.Errorf("model is required")
	}
	if err := ValidateMessages(messages); err != nil {
		return nil, err
	}
	if apiConfig == nil || apiConfig.APIKey == nil || strings.TrimSpace(*apiConfig.APIKey) == "" {
		return nil, fmt.Errorf("API key is nil")
	}
	chatModelConfig = normalizeChatConfig(chatModelConfig)

	baseURL, err := resolveModelBaseURL(m.BaseURL, apiConfig.Region)
	if err != nil {
		return nil, err
	}
	if m.URLSuffix.Chat == "" {
		return nil, fmt.Errorf("chat endpoint is missing")
	}
	url := joinModelURL(baseURL, m.URLSuffix.Chat)

	// Build request body
	reqBody := map[string]interface{}{
		"model":       modelName,
		"messages":    messages,
		"stream":      false,
		"temperature": 1,
	}

	if chatModelConfig.MaxTokens != nil {
		reqBody["max_tokens"] = *chatModelConfig.MaxTokens
	}

	if chatModelConfig.Temperature != nil {
		reqBody["temperature"] = *chatModelConfig.Temperature
	}

	if chatModelConfig.TopP != nil {
		reqBody["top_p"] = *chatModelConfig.TopP
	}

	if chatModelConfig.Stop != nil {
		reqBody["stop"] = *chatModelConfig.Stop
	}

	if chatModelConfig.Thinking != nil {
		if *chatModelConfig.Thinking {
			var thinkingFlag string
			switch *chatModelConfig.Effort {
			case "none":
				thinkingFlag = "disabled"
				chatModelConfig.Thinking = nil
				break
			case "low":
				thinkingFlag = "disabled"
				chatModelConfig.Thinking = nil
				break
			case "medium":
				thinkingFlag = "disabled"
				chatModelConfig.Thinking = nil
				break
			case "high":
				thinkingFlag = "enabled"
				reqBody["reasoning_effort"] = "high"
				break
			case "default":
				thinkingFlag = "enabled"
				reqBody["reasoning_effort"] = "high"
				break
			case "max":
				thinkingFlag = "enabled"
				reqBody["reasoning_effort"] = "max"
				break
			default:
				return nil, fmt.Errorf("invalid effort level")
			}
			reqBody["thinking"] = map[string]interface{}{
				"type": thinkingFlag,
			}
		} else {
			reqBody["thinking"] = map[string]interface{}{
				"type": "disabled",
			}
		}
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(requestContext(apiConfig), "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*apiConfig.APIKey))

	resp, err := m.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("API request failed with status %d", resp.StatusCode)
	}
	response, err := decodeLegacyChatResponse(resp.Body)
	if ctxErr := requestContext(apiConfig).Err(); ctxErr != nil {
		return nil, ctxErr
	}
	if err != nil {
		return nil, err
	}
	if response.ReasoningContent != nil {
		reason := strings.TrimPrefix(*response.ReasoningContent, "\n")
		response.ReasoningContent = &reason
	}
	return response, nil
}

func (m *DeepSeekModel) ChatStreamly(modelName, apiKey, message *string, genConf map[string]interface{}) (<-chan string, error) {
	return nil, fmt.Errorf("not implemented")
}

func (m *DeepSeekModel) ChatStreamlyWithChannel(modelName, apiKey, message *string, genConf map[string]interface{}, resultChan chan<- string) error {
	return fmt.Errorf("not implemented")
}

func (m *DeepSeekModel) ChatStreamlyWithSender(modelName, message *string, apiConfig *APIConfig, chatModelConfig *ChatConfig, sender func(*string, *string) error) error {
	if modelName == nil || message == nil {
		return fmt.Errorf("model and message are required")
	}
	return m.ChatStreamlyWithMessages(*modelName, []Message{{Role: "user", Content: *message}}, apiConfig, chatModelConfig, sender)
}
func (m *DeepSeekModel) ChatStreamlyWithMessages(modelName string, messages []Message, apiConfig *APIConfig, chatModelConfig *ChatConfig, sender func(*string, *string) error) error {
	apiConfig, cancelCall := modelStreamConfig(apiConfig)
	defer cancelCall()
	if sender == nil || apiConfig == nil || apiConfig.APIKey == nil || strings.TrimSpace(*apiConfig.APIKey) == "" {
		return fmt.Errorf("message or API key is nil")
	}
	if strings.TrimSpace(modelName) == "" {
		return fmt.Errorf("model is required")
	}
	if err := ValidateMessages(messages); err != nil {
		return err
	}
	chatModelConfig = normalizeChatConfig(chatModelConfig)
	baseURL, err := resolveModelBaseURL(m.BaseURL, apiConfig.Region)
	if err != nil {
		return err
	}
	if m.URLSuffix.Chat == "" {
		return fmt.Errorf("chat endpoint is missing")
	}
	url := joinModelURL(baseURL, m.URLSuffix.Chat)

	// Build request body with streaming enabled
	reqBody := map[string]interface{}{
		"model":       modelName,
		"messages":    messages,
		"stream":      true,
		"temperature": 1,
	}

	if chatModelConfig.MaxTokens != nil {
		reqBody["max_tokens"] = *chatModelConfig.MaxTokens
	}

	if chatModelConfig.Temperature != nil {
		reqBody["temperature"] = *chatModelConfig.Temperature
	}

	if chatModelConfig.DoSample != nil {
		reqBody["do_sample"] = *chatModelConfig.DoSample
	}

	if chatModelConfig.TopP != nil {
		reqBody["top_p"] = *chatModelConfig.TopP
	}

	if chatModelConfig.Stop != nil {
		reqBody["stop"] = *chatModelConfig.Stop
	}

	if chatModelConfig.Thinking != nil {
		if *chatModelConfig.Thinking {
			var thinkingFlag string
			switch *chatModelConfig.Effort {
			case "none":
				thinkingFlag = "disabled"
				chatModelConfig.Thinking = nil
				break
			case "low":
				thinkingFlag = "disabled"
				chatModelConfig.Thinking = nil
				break
			case "medium":
				thinkingFlag = "disabled"
				chatModelConfig.Thinking = nil
				break
			case "high":
				thinkingFlag = "enabled"
				reqBody["reasoning_effort"] = "high"
				break
			case "default":
				thinkingFlag = "enabled"
				reqBody["reasoning_effort"] = "high"
				break
			case "max":
				thinkingFlag = "enabled"
				reqBody["reasoning_effort"] = "max"
				break
			default:
				return fmt.Errorf("invalid effort level")
			}
			reqBody["thinking"] = map[string]interface{}{
				"type": thinkingFlag,
			}
		} else {
			reqBody["thinking"] = map[string]interface{}{
				"type": "disabled",
			}
		}
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return fmt.Errorf("failed to marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(requestContext(apiConfig), "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*apiConfig.APIKey))

	resp, err := m.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("API request failed with status %d", resp.StatusCode)
	}

	return readLegacyChatStream(resp.Body, apiConfig, false, sender)
}

func (m *DeepSeekModel) Embed(modelName *string, texts []string, apiConfig *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	suffix := m.URLSuffix.Embedding
	if suffix == "" {
		suffix = "embeddings"
	}
	return embedHTTPWithConfig(m.httpClient, m.BaseURL, suffix, modelName, texts, apiConfig, embeddingConfig)
}

func (m *DeepSeekModel) ListModels(apiConfig *APIConfig) ([]string, error) {
	apiConfig, cancelCall := modelCallConfig(apiConfig)
	defer cancelCall()
	if apiConfig == nil || apiConfig.APIKey == nil || strings.TrimSpace(*apiConfig.APIKey) == "" {
		return nil, fmt.Errorf("API key is nil")
	}
	baseURL, err := resolveModelBaseURL(m.BaseURL, apiConfig.Region)
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequestWithContext(requestContext(apiConfig), http.MethodGet, joinModelURL(baseURL, m.URLSuffix.Models), nil)
	if err != nil {
		return nil, fmt.Errorf("create model list request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*apiConfig.APIKey))

	resp, err := m.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("list DeepSeek models: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("list DeepSeek models: status %d", resp.StatusCode)
	}

	names, err := strictProviderModelNames(resp.Body)
	if ctxErr := requestContext(apiConfig).Err(); ctxErr != nil {
		return nil, ctxErr
	}
	if err != nil {
		return nil, err
	}
	return names, requestContext(apiConfig).Err()
}

type deepSeekBalanceResponse struct {
	IsAvailable  *bool `json:"is_available"`
	BalanceInfos []struct {
		Currency        string `json:"currency"`
		TotalBalance    string `json:"total_balance"`
		GrantedBalance  string `json:"granted_balance"`
		ToppedUpBalance string `json:"topped_up_balance"`
	} `json:"balance_infos"`
	Error json.RawMessage `json:"error"`
}

// Balance preserves each currency and account availability. The first pair also
// supplies the common balance/currency fields consumed by the provider CLI.
func (m *DeepSeekModel) Balance(apiConfig *APIConfig) (map[string]interface{}, error) {
	apiConfig, cancelCall := modelCallConfig(apiConfig)
	defer cancelCall()
	var result deepSeekBalanceResponse
	if err := decodeBalanceHTTP(m.httpClient, m.BaseURL, m.URLSuffix.Balance, apiConfig, &result); err != nil {
		return nil, fmt.Errorf("deepseek: %w", err)
	}
	if result.IsAvailable == nil || len(result.Error) != 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("deepseek: balance provider returned an error")
	}
	if len(result.BalanceInfos) == 0 {
		return nil, fmt.Errorf("deepseek: no balance info in response")
	}
	infos := make([]map[string]interface{}, 0, len(result.BalanceInfos))
	for _, item := range result.BalanceInfos {
		if strings.TrimSpace(item.Currency) == "" {
			return nil, fmt.Errorf("deepseek: balance currency is missing")
		}
		total, err := parseBalanceAmount(item.TotalBalance)
		if err != nil {
			return nil, fmt.Errorf("deepseek: invalid total balance: %w", err)
		}
		info := map[string]interface{}{"currency": item.Currency, "total_balance": total}
		for field, raw := range map[string]string{"granted_balance": item.GrantedBalance, "topped_up_balance": item.ToppedUpBalance} {
			if raw == "" {
				continue
			}
			amount, err := parseBalanceAmount(raw)
			if err != nil {
				return nil, fmt.Errorf("deepseek: invalid %s: %w", field, err)
			}
			info[field] = amount
		}
		infos = append(infos, info)
	}
	return map[string]interface{}{
		"balance":       infos[0]["total_balance"],
		"currency":      infos[0]["currency"],
		"is_available":  *result.IsAvailable,
		"balance_infos": infos,
	}, nil
}

func (m *DeepSeekModel) CheckConnection(apiConfig *APIConfig) error {
	_, err := m.ListModels(apiConfig)
	return err
}

func resolveModelBaseURL(baseURLs map[string]string, region *string) (string, error) {
	regionName := "default"
	if region != nil && *region != "" {
		regionName = *region
	}
	baseURL := strings.TrimRight(baseURLs[regionName], "/")
	if baseURL == "" {
		return "", fmt.Errorf("no base URL configured for region %q", regionName)
	}
	return baseURL, nil
}

func (m *DeepSeekModel) Rerank(modelName *string, query string, texts []string, apiConfig *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return nil, fmt.Errorf("%s: rerank is not supported", m.Name())
}
