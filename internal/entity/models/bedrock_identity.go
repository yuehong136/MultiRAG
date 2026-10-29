package models

// ResolveEmbeddingEndpoint returns the runtime endpoint and effective signing
// region selected by Embed without resolving AWS credentials or making a call.
func (b *BedrockModel) ResolveEmbeddingEndpoint(modelName string, api *APIConfig) (endpoint, region string, err error) {
	_, region, err = parseBedrockKey(api)
	if err != nil {
		return "", "", err
	}
	endpoint, err = b.endpoint(region, modelName, bedrockSuffix(b.URLSuffix.Embedding, "invoke"), false)
	if err != nil {
		return "", "", err
	}
	return endpoint, region, nil
}
