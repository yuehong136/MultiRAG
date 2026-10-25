package models

import "time"

// AstraflowModel implements hosted chat, discovery, embeddings, reranking and speech.
type AstraflowModel struct{ *openAIChatDriver }

func NewAstraflowModel(urls map[string]string, suffix URLSuffix) *AstraflowModel {
	d := newOpenAIChatDriver(urls, suffix, "astraflow", hostedChatBody)
	d.reasoningAlias, d.strictDiscovery, d.strictStream = true, true, true
	d.streamIdleTimeout = 60 * time.Second
	d.reasoningFallback = true
	d.terminalWithoutDelta = true
	d.httpClient.CheckRedirect = rejectModelRedirect
	return &AstraflowModel{d}
}

var _ ModelDriver = (*AstraflowModel)(nil)

func (a *AstraflowModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(a.httpClient, a.BaseURL, a.URLSuffix.Embedding, name, texts, api, config, embeddingHTTPOptions{rejectNonzeroCode: true})
}

func (a *AstraflowModel) Rerank(name *string, query string, documents []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	return rerankHTTP(a.httpClient, a.BaseURL, a.URLSuffix.Rerank, name, query, documents, api, config, nil)
}

func (a *AstraflowModel) AudioSpeech(name *string, text string, api *APIConfig, config *TTSConfig) (*TTSResponse, error) {
	body, format, err := speechInput(name, text, config)
	if err != nil {
		return nil, err
	}
	delete(body, "response_format")
	delete(body, "audio_setting")
	body["model"], body["input"], body["format"] = *name, text, format
	if config != nil && config.Voice != "" {
		body["voice"] = config.Voice
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(a.httpClient, a.BaseURL, a.URLSuffix.TTS, local, body, "")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	result, err := readSpeechBytes(resp, format)
	if err != nil {
		return nil, err
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return result, nil
}

var _ TTSDriver = (*AstraflowModel)(nil)
