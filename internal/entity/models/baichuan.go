package models

import "fmt"

// BaichuanModel provides chat and strictly validated batched embeddings.
type BaichuanModel struct{ *openAIChatDriver }

func NewBaichuanModel(baseURL map[string]string, suffix URLSuffix) *BaichuanModel {
	return &BaichuanModel{newOpenAIChatDriver(baseURL, suffix, "baichuan", openAIChatBody)}
}

func (b *BaichuanModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(b.httpClient, b.BaseURL, b.URLSuffix.Embedding, name, texts, api, config)
}

func (b *BaichuanModel) ListModels(api *APIConfig) ([]string, error) {
	return nil, fmt.Errorf("baichuan: model discovery is unsupported")
}

func (b *BaichuanModel) CheckConnection(api *APIConfig) error {
	return fmt.Errorf("baichuan: connection checking is unsupported")
}

var _ ModelDriver = (*BaichuanModel)(nil)
