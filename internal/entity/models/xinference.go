package models

import (
	"fmt"
	"maps"
	"strings"
)

// XinferenceModel reuses the local OpenAI transport, optional authentication,
// reasoning fields and idle-bounded streams for this chat-only provider batch.
type XinferenceModel struct{ *LocalAIModel }

func NewXinferenceModel(urls map[string]string, suffix URLSuffix) *XinferenceModel {
	base := maps.Clone(urls)
	for region, endpoint := range base {
		base[region] = strings.TrimSuffix(strings.TrimRight(strings.TrimSpace(endpoint), "/"), "/v1")
	}
	return &XinferenceModel{NewLocalAIModel(base, suffix)}
}

func (x *XinferenceModel) Name() string { return "xinference" }

func xinferenceChatConfig(config *ChatConfig) *ChatConfig {
	if config == nil {
		return nil
	}
	local := *config
	local.Thinking, local.Effort = nil, nil
	return &local
}

func (x *XinferenceModel) ChatWithMessages(name string, api *APIConfig, messages []Message, config *ChatConfig) (*ChatResponse, error) {
	return x.LocalAIModel.ChatWithMessages(name, api, messages, xinferenceChatConfig(config))
}

func (x *XinferenceModel) ChatStreamlyWithMessages(name string, messages []Message, api *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	return x.LocalAIModel.ChatStreamlyWithMessages(name, messages, api, xinferenceChatConfig(config), sender)
}

func (x *XinferenceModel) ChatStreamlyWithSender(name, message *string, api *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if name == nil || message == nil {
		return fmt.Errorf("xinference: model and message are required")
	}
	return x.ChatStreamlyWithMessages(*name, []Message{{Role: "user", Content: *message}}, api, config, sender)
}

func (x *XinferenceModel) Embed(*string, []string, *APIConfig, *EmbeddingConfig) ([]EmbeddingData, error) {
	return nil, fmt.Errorf("%w: xinference embedding", ErrUnsupportedCapability)
}

func (x *XinferenceModel) Rerank(*string, string, []string, *APIConfig, *RerankConfig) (*RerankResponse, error) {
	return nil, fmt.Errorf("%w: xinference rerank", ErrUnsupportedCapability)
}

var _ ModelDriver = (*XinferenceModel)(nil)
