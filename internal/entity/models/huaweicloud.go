package models

import (
	"fmt"
	"strings"
	"time"
)

// HuaweiCloudModel retains model-specific routes while sharing strict wire validation.
type HuaweiCloudModel struct{ *openAIChatDriver }

func NewHuaweiCloudModel(urls map[string]string, suffix URLSuffix) *HuaweiCloudModel {
	d := newGatewayChatDriver(urls, suffix, "huaweicloud")
	d.buildBody = huaweiCloudChatBody
	d.sseEvents = true
	d.streamIdleTimeout = 60 * time.Second
	d.httpClient.CheckRedirect = rejectModelRedirect
	return &HuaweiCloudModel{d}
}
func (h *HuaweiCloudModel) NewInstance(urls map[string]string) ModelDriver {
	return NewHuaweiCloudModel(urls, h.URLSuffix)
}

func huaweiCloudAPI(api *APIConfig) *APIConfig {
	local := APIConfig{}
	if api != nil {
		local = *api
	}
	if local.APIKey != nil {
		key := strings.TrimSpace(*local.APIKey)
		if strings.HasPrefix(strings.ToLower(key), "bearer ") {
			key = strings.TrimSpace(key[len("bearer "):])
		}
		local.APIKey = &key
	}
	return &local
}

// DeepSeek-V3 uses the V1 endpoint and lowercase wire ID; other models use V2.
func (h *HuaweiCloudModel) chatDriver(name string) *openAIChatDriver {
	d := *h.openAIChatDriver
	base := *d.DummyModel
	d.DummyModel = &base
	if strings.EqualFold(name, "deepseek-v3") {
		d.URLSuffix.Chat = d.URLSuffix.AsyncChat
		if d.URLSuffix.Chat == "" {
			d.URLSuffix.Chat = "v1/chat/completions"
		}
	}
	return &d
}
func (h *HuaweiCloudModel) ChatWithMessages(name string, api *APIConfig, messages []Message, config *ChatConfig) (*ChatResponse, error) {
	response, err := h.chatDriver(name).ChatWithMessages(name, huaweiCloudAPI(api), messages, config)
	if err == nil && response.ReasoningContent != nil {
		reason := strings.TrimPrefix(*response.ReasoningContent, "\n")
		response.ReasoningContent = &reason
	}
	return response, err
}
func (h *HuaweiCloudModel) ChatStreamlyWithMessages(name string, messages []Message, api *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	return h.chatDriver(name).ChatStreamlyWithMessages(name, messages, huaweiCloudAPI(api), config, sender)
}
func (h *HuaweiCloudModel) ChatStreamlyWithSender(name, message *string, api *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if name == nil || message == nil {
		return fmt.Errorf("model name and message are required")
	}
	return h.ChatStreamlyWithMessages(*name, []Message{{Role: "user", Content: *message}}, api, config, sender)
}
func (h *HuaweiCloudModel) ListModels(api *APIConfig) ([]string, error) {
	return h.openAIChatDriver.ListModels(huaweiCloudAPI(api))
}
func (h *HuaweiCloudModel) CheckConnection(api *APIConfig) error {
	_, err := h.ListModels(api)
	return err
}
func (h *HuaweiCloudModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(h.httpClient, h.BaseURL, h.URLSuffix.Embedding, name, texts, huaweiCloudAPI(api), config, embeddingHTTPOptions{rejectNonzeroCode: true})
}
func (h *HuaweiCloudModel) Rerank(name *string, query string, texts []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	if strings.TrimSpace(query) == "" {
		return nil, fmt.Errorf("query is required")
	}
	top, err := RerankTopN(len(texts), config)
	if err != nil {
		return nil, err
	}
	// Huawei returns all scores; validate the complete mapping before selecting TopN.
	response, err := rerankHTTP(h.httpClient, h.BaseURL, h.URLSuffix.Rerank, name, query, texts, huaweiCloudAPI(api), nil, map[string]any{"top_n": nil, "return_documents": nil})
	if err != nil {
		return nil, err
	}
	response.Data = response.Data[:top]
	return response, nil
}
func huaweiCloudChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	body, err := validatedHostedChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	if strings.EqualFold(name, "deepseek-v3") {
		body["model"] = strings.ToLower(name)
	}
	if config == nil || config.Thinking == nil {
		return body, nil
	}
	model := strings.ToLower(name)
	supported := false
	for _, token := range []string{"deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v3.1", "deepseek-v3.2", "qwen3-235b-a22b", "qwen3-32b", "qwen3-30b-a3b", "glm-5.1", "glm-5", "kimi-k2.6"} {
		if strings.Contains(model, token) {
			supported = true
			break
		}
	}
	if !supported {
		return body, nil
	}
	thinking := "disabled"
	if *config.Thinking {
		thinking = "enabled"
		if strings.Contains(model, "deepseek-v4-pro") || strings.Contains(model, "deepseek-v4-flash") {
			effort := "high"
			if config.Effort != nil && *config.Effort != "" {
				effort = strings.ToLower(*config.Effort)
			}
			switch effort {
			case "none", "low", "medium":
				thinking = "disabled"
			case "max":
				body["reasoning_effort"] = "max"
			default:
				body["reasoning_effort"] = "high"
			}
		}
	}
	body["thinking"] = map[string]any{"type": thinking}
	return body, nil
}

var _ ModelDriver = (*HuaweiCloudModel)(nil)
