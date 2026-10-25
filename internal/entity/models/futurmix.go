package models

import (
	"fmt"
	"time"
)

// FuturMixModel implements the documented OpenAI-compatible chat surface.
// Discovery is a static catalog: no undocumented endpoint or paid probe is used.
type FuturMixModel struct{ *openAIChatDriver }

func NewFuturMixModel(urls map[string]string, suffix URLSuffix) *FuturMixModel {
	d := newOpenAIChatDriver(urls, suffix, "futurmix", futurMixChatBody)
	d.strictStream, d.terminalWithoutDelta, d.sseEvents = true, true, true
	d.streamIdleTimeout = 60 * time.Second
	d.httpClient.CheckRedirect = rejectModelRedirect
	return &FuturMixModel{d}
}
func futurMixChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	if err := validateHostedGeneration(config); err != nil {
		return nil, err
	}
	if stream && config != nil && config.Stream != nil && !*config.Stream {
		return nil, fmt.Errorf("futurmix: stream must be true")
	}
	return openAIChatBody(name, messages, config, stream)
}
func (f *FuturMixModel) ListModels(api *APIConfig) ([]string, error) {
	return nil, fmt.Errorf("futurmix: dynamic model discovery is unsupported; use the catalog")
}
func (f *FuturMixModel) CheckConnection(api *APIConfig) error {
	return fmt.Errorf("futurmix: connection check is unsupported")
}
func (f *FuturMixModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return nil, fmt.Errorf("futurmix: embedding is unsupported")
}
func (f *FuturMixModel) Rerank(name *string, query string, texts []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	return nil, fmt.Errorf("futurmix: rerank is unsupported")
}
func (f *FuturMixModel) Balance(api *APIConfig) (map[string]any, error) {
	return nil, fmt.Errorf("futurmix: balance is unsupported")
}

var _ ModelDriver = (*FuturMixModel)(nil)
