package models

// GroqModel implements the provider's chat and discovery endpoints.
type GroqModel struct{ *openAIChatDriver }

func NewGroqModel(urls map[string]string, suffix URLSuffix) *GroqModel {
	d := newGatewayChatDriver(urls, suffix, "groq")
	return &GroqModel{d}
}

var _ ModelDriver = (*GroqModel)(nil)
