// Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
// Licensed under the Apache License, Version 2.0.
package models

import "strings"

// JinaModel implements embeddings, rerank and public model discovery. Chat is
// intentionally unavailable in this catalog batch.
type JinaModel struct{ *openAIChatDriver }

func NewJinaModel(urls map[string]string, suffix URLSuffix) *JinaModel {
	d := newOpenAIChatDriver(urls, suffix, "jina", openAIChatBody)
	d.optionalDiscoveryKey = true
	return &JinaModel{d}
}

func (j *JinaModel) ChatWithMessages(name string, api *APIConfig, messages []Message, config *ChatConfig) (*ChatResponse, error) {
	return j.DummyModel.ChatWithMessages(name, api, messages, config)
}
func (j *JinaModel) ChatStreamlyWithSender(name, message *string, api *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	return j.DummyModel.ChatStreamlyWithSender(name, message, api, config, sender)
}
func (j *JinaModel) ChatStreamlyWithMessages(name string, messages []Message, api *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	return j.DummyModel.ChatStreamlyWithMessages(name, messages, api, config, sender)
}
func (j *JinaModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	options := embeddingHTTPOptions{}
	// Task-aware Jina models distinguish document and query embeddings. Older
	// v2/CLIP models retain their original payload without an unsupported task.
	if name != nil && (strings.HasPrefix(*name, "jina-embeddings-v3") || strings.HasPrefix(*name, "jina-embeddings-v4") || strings.HasPrefix(*name, "jina-embeddings-v5")) {
		task := "retrieval.passage"
		if config != nil && config.Query {
			task = "retrieval.query"
		}
		options.parameters = map[string]any{"task": task}
	}
	return embedHTTPWithConfig(j.httpClient, j.BaseURL, j.URLSuffix.Embedding, name, texts, api, config, options)
}
func (j *JinaModel) Rerank(name *string, query string, texts []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	return rerankHTTP(j.httpClient, j.BaseURL, j.URLSuffix.Rerank, name, query, texts, api, config, nil)
}

var _ ModelDriver = (*JinaModel)(nil)
