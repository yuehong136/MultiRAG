package models

import "strings"

type providerModel struct {
	ID      string `json:"id"`
	OwnedBy string `json:"owned_by"`
}

type providerModelList struct {
	Models []providerModel `json:"data"`
}

// NormalizeModelFamily normalizes model classes and names for shared response parsing.
func NormalizeModelFamily(modelClass *string) string {
	if modelClass == nil {
		return ""
	}
	family := strings.ToLower(strings.TrimSpace(*modelClass))
	if separator := strings.LastIndex(family, "/"); separator >= 0 {
		family = family[separator+1:]
	}
	if family == "qwen3" || strings.HasPrefix(family, "qwen3-") || strings.HasPrefix(family, "qwen3.") {
		return "qwen3"
	}
	family, _, _ = strings.Cut(family, "-")
	return family
}

// ModelClassFromName derives a lowercase family from the model ID, excluding its namespace.
func ModelClassFromName(name string) string {
	return NormalizeModelFamily(&name)
}

func chatModelClass(name string, config *ChatConfig) string {
	if config != nil && config.ModelClass != nil && strings.TrimSpace(*config.ModelClass) != "" {
		return NormalizeModelFamily(config.ModelClass)
	}
	return ModelClassFromName(name)
}

func normalizeChatConfig(config *ChatConfig) *ChatConfig {
	local := ChatConfig{}
	if config != nil {
		local = *config
	}
	config = &local
	if config.Effort == nil {
		defaultEffort := "default"
		config.Effort = &defaultEffort
	}
	if config.Verbosity == nil {
		defaultVerbosity := "low"
		config.Verbosity = &defaultVerbosity
	}
	return config
}

// GetThinkingAndAnswer separates provider-specific inline reasoning markup.
func GetThinkingAndAnswer(modelClass *string, content *string) (*string, *string) {
	if modelClass == nil || content == nil {
		return nil, content
	}
	if NormalizeModelFamily(modelClass) == "qwen3" {
		return extractThinkContent(content)
	}
	return nil, content
}

func extractThinkContent(content *string) (*string, *string) {
	if content == nil {
		return nil, nil
	}
	startTag := "<think>"
	endTag := "</think>"
	startIndex := strings.Index(*content, startTag)
	endIndex := strings.Index(*content, endTag)
	if startIndex == -1 || endIndex == -1 || endIndex <= startIndex {
		return nil, content
	}

	thinking := (*content)[startIndex+len(startTag) : endIndex]
	answer := (*content)[endIndex+len(endTag):]
	thinking = strings.TrimLeft(thinking, "\n")
	answer = strings.TrimLeft(answer, "\n")
	return &thinking, &answer
}
