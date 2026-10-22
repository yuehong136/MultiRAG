package models

// OpenAIModel supports chat, discovery and validated embeddings with optional dimensions.
type OpenAIModel struct{ *openAIChatDriver }

func NewOpenAIModel(baseURL map[string]string, suffix URLSuffix) *OpenAIModel {
	if suffix.Embedding == "" {
		suffix.Embedding = "embeddings"
	}
	return &OpenAIModel{newOpenAIChatDriver(baseURL, suffix, "openai", openAIChatBody)}
}

func (o *OpenAIModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	return embedHTTPWithConfig(o.httpClient, o.BaseURL, o.URLSuffix.Embedding, name, texts, local, config)
}

var _ ModelDriver = (*OpenAIModel)(nil)
