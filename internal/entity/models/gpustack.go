package models

import (
	"fmt"
	"maps"
	"strings"
	"time"
)

// GPUStackModel supports tenant-hosted chat, embeddings and discovery with a required key.
type GPUStackModel struct{ *openAIChatDriver }

func NewGPUStackModel(urls map[string]string, suffix URLSuffix) *GPUStackModel {
	base := maps.Clone(urls)
	for region, endpoint := range base {
		base[region] = strings.TrimSuffix(strings.TrimRight(strings.TrimSpace(endpoint), "/"), "/v1")
	}
	d := newOpenAIChatDriver(base, suffix, "gpustack", gpuStackChatBody)
	d.strictDiscovery, d.streamIdleTimeout = true, 60*time.Second
	d.strictStream = true
	d.httpClient.CheckRedirect = rejectModelRedirect
	return &GPUStackModel{d}
}

func gpuStackChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	if stream && config != nil && config.Stream != nil && !*config.Stream {
		return nil, fmt.Errorf("gpustack: stream must be true")
	}
	return openAIChatBody(name, messages, config, stream)
}

// Embed uses the catalog's v1-openai endpoint and preserves strict indexed batches.
func (g *GPUStackModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(g.httpClient, g.BaseURL, g.URLSuffix.Embedding, name, texts, api, config)
}

var _ ModelDriver = (*GPUStackModel)(nil)
