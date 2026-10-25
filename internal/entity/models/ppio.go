package models

// PPIOModel implements the provider's chat and discovery endpoints.
type PPIOModel struct{ *openAIChatDriver }

func NewPPIOModel(urls map[string]string, suffix URLSuffix) *PPIOModel {
	d := newGatewayChatDriver(urls, suffix, "ppio")
	return &PPIOModel{d}
}

var _ ModelDriver = (*PPIOModel)(nil)
