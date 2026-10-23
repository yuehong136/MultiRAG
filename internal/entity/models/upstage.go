package models

import "strings"

// UpstageModel shares chat transport and preserves Solar reasoning fields.
type UpstageModel struct{ *openAIChatDriver }

func NewUpstageModel(baseURL map[string]string, suffix URLSuffix) *UpstageModel {
	driver := newOpenAIChatDriver(baseURL, suffix, "upstage", upstageChatBody)
	// Solar uses message.reasoning / delta.reasoning, also used by OpenRouter.
	driver.openRouterReasoning = true
	return &UpstageModel{driver}
}

func upstageChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	body, err := stepFunChatBody(name, messages, config, stream)
	if err == nil && config != nil && config.Effort != nil && *config.Effort != "" {
		body["reasoning_effort"] = *config.Effort
	}
	return body, err
}

func (u *UpstageModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	// The family selects a different model for queries and stored passages.
	// Copy the name so concurrent calls never alter the caller's selection.
	if name != nil {
		selected := *name
		family := strings.TrimSuffix(strings.TrimSuffix(selected, "-query"), "-passage")
		if family == "solar-embedding-1-large" {
			selected = family + "-passage"
			if config != nil && config.Query {
				selected = family + "-query"
			}
		}
		name = &selected
	}
	return embedHTTPWithConfig(u.httpClient, u.BaseURL, u.URLSuffix.Embedding, name, texts, api, config)
}

var _ ModelDriver = (*UpstageModel)(nil)
