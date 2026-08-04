package models

import "strings"

type providerModel struct {
	ID      string `json:"id"`
	OwnedBy string `json:"owned_by"`
}

type providerModelList struct {
	Models []providerModel `json:"data"`
}

func normalizeChatConfig(config *ChatConfig) *ChatConfig {
	if config == nil {
		config = &ChatConfig{}
	}
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
func GetThinkingAndAnswer(modelSeries *string, content *string) (*string, *string) {
	if modelSeries == nil || content == nil {
		return nil, content
	}
	if *modelSeries == "qwen3" {
		return extractThinkContent(content)
	}
	return nil, content
}

func extractThinkContent(content *string) (*string, *string) {
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
