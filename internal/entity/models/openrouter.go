package models

// OpenRouterModel uses the shared chat transport and OpenRouter reasoning fields.
type OpenRouterModel struct{ *openAIChatDriver }

func NewOpenRouterModel(baseURL map[string]string, suffix URLSuffix) *OpenRouterModel {
	driver := newOpenAIChatDriver(baseURL, suffix, "openrouter", openRouterChatBody)
	driver.openRouterReasoning = true
	return &OpenRouterModel{driver}
}

func openRouterChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	body, err := openAIChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	history := make([]map[string]any, len(messages))
	for i, message := range messages {
		history[i] = map[string]any{"role": message.Role, "content": message.Content}
		if message.ReasoningContent != nil {
			history[i]["reasoning"] = *message.ReasoningContent
		}
	}
	body["messages"] = history
	if config != nil {
		if config.Thinking != nil {
			body["reasoning"] = map[string]any{"enabled": *config.Thinking}
		}
		if config.Effort != nil && *config.Effort != "" && *config.Effort != "default" && (config.Thinking == nil || *config.Thinking) {
			body["reasoning"] = map[string]any{"effort": *config.Effort}
		}
	}
	return body, nil
}

var _ ModelDriver = (*OpenRouterModel)(nil)
