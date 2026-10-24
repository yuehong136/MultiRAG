package models

import (
	"fmt"
	"time"
)

type N1NModel struct{ *openAIChatDriver }

func NewN1NModel(urls map[string]string, suffix URLSuffix) *N1NModel {
	d := newOpenAIChatDriver(urls, suffix, "n1n", n1nChatBody)
	d.strictDiscovery, d.streamIdleTimeout = true, 60*time.Second
	d.strictStream = true
	d.httpClient.CheckRedirect = rejectModelRedirect
	return &N1NModel{d}
}

func n1nChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	if stream && config != nil && config.Stream != nil && !*config.Stream {
		return nil, fmt.Errorf("n1n: stream must be true")
	}
	body, err := openAIChatBody(name, messages, config, stream)
	if err == nil && config != nil && config.Thinking != nil {
		mode := "disabled"
		if *config.Thinking {
			mode = "enabled"
		}
		body["thinking"] = map[string]any{"type": mode}
	}
	return body, err
}

func (n *N1NModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(n.httpClient, n.BaseURL, n.URLSuffix.Embedding, name, texts, api, config)
}

func (n *N1NModel) Rerank(name *string, query string, texts []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	return rerankHTTP(n.httpClient, n.BaseURL, n.URLSuffix.Rerank, name, query, texts, api, config, map[string]any{"return_documents": nil})
}

var _ ModelDriver = (*N1NModel)(nil)
