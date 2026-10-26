package models

import (
	"fmt"
	"strings"
	"time"
)

type JieKouAIModel struct{ *openAIChatDriver }

func NewJieKouAIModel(urls map[string]string, suffix URLSuffix) *JieKouAIModel {
	suffix.Chat = strings.Trim(strings.TrimSpace(suffix.Chat), "/")
	suffix.Models = strings.Trim(strings.TrimSpace(suffix.Models), "/")
	suffix.Embedding = strings.Trim(strings.TrimSpace(suffix.Embedding), "/")
	suffix.Rerank = strings.Trim(strings.TrimSpace(suffix.Rerank), "/")
	driver := newOpenAIChatDriver(urls, suffix, "jiekouai", jieKouAIChatBody)
	driver.accept = "application/json"
	driver.strictStream, driver.terminalWithoutDelta = true, true
	driver.streamIdleTimeout = 60 * time.Second
	return &JieKouAIModel{driver}
}

func jieKouAIChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	// The selected method controls the wire mode, including conflicting options.
	body, err := openAIChatBody(strings.TrimSpace(name), messages, config, stream)
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

// Copy the normalized name; caller-owned configuration may be used concurrently.
func jieKouAIModelName(name *string) *string {
	if name == nil {
		return nil
	}
	value := strings.TrimSpace(*name)
	return &value
}

func (j *JieKouAIModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(j.httpClient, j.BaseURL, j.URLSuffix.Embedding, jieKouAIModelName(name), texts, api, config)
}

func (j *JieKouAIModel) Rerank(name *string, query string, texts []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	if len(texts) > 0 && strings.TrimSpace(query) == "" {
		return nil, fmt.Errorf("jiekouai: rerank query is required")
	}
	return rerankHTTP(j.httpClient, j.BaseURL, j.URLSuffix.Rerank, jieKouAIModelName(name), strings.TrimSpace(query), texts, api, config, map[string]any{"return_documents": nil})
}

func (j *JieKouAIModel) ListModels(api *APIConfig) ([]string, error) {
	return listStrictHostedModels(j.openAIChatDriver, api)
}

func (j *JieKouAIModel) CheckConnection(api *APIConfig) error {
	_, err := j.ListModels(api)
	return err
}

var _ ModelDriver = (*JieKouAIModel)(nil)
