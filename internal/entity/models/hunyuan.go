package models

import "time"

// HunyuanModel retains tenant-specific endpoints and the provider's OpenAI wire contract.
type HunyuanModel struct{ *openAIChatDriver }

func NewHunyuanModel(urls map[string]string, suffix URLSuffix) *HunyuanModel {
	d := newGatewayChatDriver(urls, suffix, "hunyuan")
	d.buildBody = validatedHostedChatBody
	d.streamIdleTimeout = 60 * time.Second
	d.httpClient.CheckRedirect = rejectModelRedirect
	return &HunyuanModel{d}
}

func validatedHostedChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	if err := validateHostedGeneration(config); err != nil {
		return nil, err
	}
	body, err := gatewayChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	if config == nil || config.Temperature == nil {
		delete(body, "temperature")
	}
	return body, nil
}

var _ ModelDriver = (*HunyuanModel)(nil)

func (h *HunyuanModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(h.httpClient, h.BaseURL, h.URLSuffix.Embedding, name, texts, api, config, embeddingHTTPOptions{rejectNonzeroCode: true})
}
