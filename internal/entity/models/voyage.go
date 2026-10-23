// Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
// Licensed under the Apache License, Version 2.0.
package models

import (
	"fmt"
	"maps"
	"net/http"
)

// VoyageModel supports embeddings and reranking. Discovery, connection probes,
// chat and media stay unavailable; no implicit request consumes tenant quota.
type VoyageModel struct {
	*DummyModel
	httpClient *http.Client
}

func NewVoyageModel(urls map[string]string, suffix URLSuffix) *VoyageModel {
	return &VoyageModel{DummyModel: NewDummyModel(maps.Clone(urls), suffix), httpClient: &http.Client{Transport: providerModelTransport}}
}
func (v *VoyageModel) Name() string { return "Voyage AI" }
func (v *VoyageModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(v.httpClient, v.BaseURL, v.URLSuffix.Embedding, name, texts, api, config, embeddingHTTPOptions{dimensionField: "output_dimension"})
}
func (v *VoyageModel) Rerank(name *string, query string, texts []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	return rerankHTTP(v.httpClient, v.BaseURL, v.URLSuffix.Rerank, name, query, texts, api, config, map[string]any{"return_documents": nil}, rerankHTTPOptions{topField: "top_k", resultField: "data"})
}
func (v *VoyageModel) ListModels(api *APIConfig) ([]string, error) {
	return nil, fmt.Errorf("%w: Voyage AI discovery", ErrUnsupportedCapability)
}
func (v *VoyageModel) CheckConnection(api *APIConfig) error {
	return fmt.Errorf("%w: Voyage AI connection probe", ErrUnsupportedCapability)
}
func (v *VoyageModel) Balance(api *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("%w: Voyage AI balance", ErrUnsupportedCapability)
}

var _ ModelDriver = (*VoyageModel)(nil)
