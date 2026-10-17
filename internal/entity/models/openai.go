package models

// OpenAIModel adds chat and discovery while preserving the existing local embedding contract.
type OpenAIModel struct{ *openAIChatDriver }

func NewOpenAIModel(baseURL map[string]string, suffix URLSuffix) *OpenAIModel {
	if suffix.Embedding == "" {
		suffix.Embedding = "embeddings"
	}
	return &OpenAIModel{newOpenAIChatDriver(baseURL, suffix, "openai", openAIChatBody)}
}

// Encode is an existing local adaptation, not a capability implemented by the initial chat port.
func (o *OpenAIModel) Encode(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([][]float64, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	return encodeHTTP(o.httpClient, o.BaseURL, o.URLSuffix.Embedding, name, texts, local)
}

var _ ModelDriver = (*OpenAIModel)(nil)
