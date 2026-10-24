package models

import (
	"fmt"
	"strings"
)

type JieKouAIModel struct{ *openAIChatDriver }

func NewJieKouAIModel(urls map[string]string, suffix URLSuffix) *JieKouAIModel {
	return &JieKouAIModel{newOpenAIChatDriver(urls, suffix, "jiekouai", jieKouAIChatBody)}
}

func jieKouAIChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	if stream && config != nil && config.Stream != nil && !*config.Stream {
		return nil, fmt.Errorf("jiekouai: stream must be true")
	}
	body, err := openAIChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	if config != nil {
		if config.Thinking != nil {
			body["enable_thinking"] = *config.Thinking
		}
		if stream && config.DoSample != nil {
			body["do_sample"] = *config.DoSample
		}
	}
	return body, nil
}

func (j *JieKouAIModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(j.httpClient, j.BaseURL, j.URLSuffix.Embedding, name, texts, api, config)
}

func (j *JieKouAIModel) Rerank(name *string, query string, texts []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	if len(texts) > 0 && strings.TrimSpace(query) == "" {
		return nil, fmt.Errorf("jiekouai: rerank query is required")
	}
	return rerankHTTP(j.httpClient, j.BaseURL, j.URLSuffix.Rerank, name, query, texts, api, config, map[string]any{"return_documents": nil})
}

func (j *JieKouAIModel) ListModels(api *APIConfig) ([]string, error) {
	return listStrictHostedModels(j.openAIChatDriver, api)
}

func (j *JieKouAIModel) CheckConnection(api *APIConfig) error {
	_, err := j.ListModels(api)
	return err
}

var _ ModelDriver = (*JieKouAIModel)(nil)
