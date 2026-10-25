package models

import "time"

// TokenHubModel shares strict chat/discovery and indexed embedding validation.
type TokenHubModel struct{ *openAIChatDriver }

func NewTokenHubModel(urls map[string]string, suffix URLSuffix) *TokenHubModel {
	d := newGatewayChatDriver(urls, suffix, "tokenhub")
	d.buildBody = tokenHubChatBody
	d.streamIdleTimeout = 60 * time.Second
	d.httpClient.CheckRedirect = rejectModelRedirect
	return &TokenHubModel{d}
}
func (t *TokenHubModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(t.httpClient, t.BaseURL, t.URLSuffix.Embedding, name, texts, api, config, embeddingHTTPOptions{rejectNonzeroCode: true})
}

var _ ModelDriver = (*TokenHubModel)(nil)

func tokenHubChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	body, err := validatedHostedChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	if !stream && (config == nil || config.Temperature == nil) {
		body["temperature"] = 0.6
	}
	return body, nil
}
