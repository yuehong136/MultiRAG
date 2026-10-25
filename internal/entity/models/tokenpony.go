package models

// TokenPonyModel implements the provider's chat and discovery endpoints.
type TokenPonyModel struct{ *openAIChatDriver }

func NewTokenPonyModel(urls map[string]string, suffix URLSuffix) *TokenPonyModel {
	d := newGatewayChatDriver(urls, suffix, "tokenpony")
	return &TokenPonyModel{d}
}

var _ ModelDriver = (*TokenPonyModel)(nil)
