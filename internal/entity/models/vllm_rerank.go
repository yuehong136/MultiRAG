package models

// Rerank uses vLLM's flat-document Jina/Cohere contract, including keyless local instances.
func (v *VLLMModel) Rerank(name *string, query string, documents []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	return rerankHTTP(v.httpClient, v.BaseURL, v.URLSuffix.Rerank, name, query, documents, api, config, nil, rerankHTTPOptions{optionalAPIKey: true})
}
