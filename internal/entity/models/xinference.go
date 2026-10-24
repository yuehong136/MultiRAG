package models

import (
	"fmt"
	"maps"
	"strings"
)

// XinferenceModel reuses the local OpenAI transport, optional authentication,
// reasoning fields, idle-bounded streams, embeddings and reranking.
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

func (x *XinferenceModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return x.LocalAIModel.Embed(name, texts, api, config)
}

func (x *XinferenceModel) Rerank(name *string, query string, texts []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	return x.LocalAIModel.Rerank(name, query, texts, api, config)
}

var _ ModelDriver = (*XinferenceModel)(nil)
