package models

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"maps"
	"net/http"
	"strings"
	"time"
)

// PerplexityModel retains the Sonar chat contract and decodes native int8
// embeddings into the floating-point vectors consumed by the local interface.
type PerplexityModel struct{ *openAIChatDriver }

func NewPerplexityModel(urls map[string]string, suffix URLSuffix) *PerplexityModel {
	base := maps.Clone(urls)
	for region, endpoint := range base {
		base[region] = strings.TrimSuffix(strings.TrimRight(strings.TrimSpace(endpoint), "/"), "/v1")
	}
	d := newOpenAIChatDriver(base, suffix, "perplexity", hostedChatBody)
	d.reasoningAlias, d.strictDiscovery, d.strictStream = true, true, true
	d.streamIdleTimeout = 60 * time.Second
	d.rootFinishReason = true
	d.reasoningFallback = true
	d.terminalWithoutDelta = true
	d.httpClient.CheckRedirect = rejectModelRedirect
	return &PerplexityModel{d}
}

func (p *PerplexityModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	if len(texts) == 0 {
		return []EmbeddingData{}, nil
	}
	if name == nil {
		return nil, fmt.Errorf("perplexity: embedding model is required")
	}
	dimension := 0
	if config != nil {
		dimension = config.Dimension
	}
	fullDimension := 0
	switch *name {
	case "pplx-embed-v1-0.6b":
		fullDimension = 1024
	case "pplx-embed-v1-4b":
		fullDimension = 2560
	default:
		return nil, fmt.Errorf("perplexity: unsupported embedding model")
	}
	if dimension != 0 && (dimension < 128 || dimension > fullDimension) {
		return nil, fmt.Errorf("perplexity: dimension must be between 128 and %d", fullDimension)
	}
	if dimension == 0 {
		dimension = fullDimension
	}
	for _, text := range texts {
		if text == "" {
			return nil, fmt.Errorf("perplexity: embedding input must be nonempty")
		}
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	vectors := make([][]float64, 0, len(texts))
	for start := 0; start < len(texts); start += 32 {
		end := min(start+32, len(texts))
		// The native API exposes base64_int8/base64_binary, not float arrays.
		body := map[string]any{"model": *name, "input": texts[start:end], "dimensions": dimension, "encoding_format": "base64_int8"}
		resp, err := p.request(http.MethodPost, p.URLSuffix.Embedding, local, body)
		if err != nil {
			return nil, err
		}
		var response struct {
			Data []struct {
				Index     *int    `json:"index"`
				Embedding *string `json:"embedding"`
			} `json:"data"`
			Error json.RawMessage `json:"error"`
		}
		err = decodeBoundedModelJSON(resp.Body, &response)
		resp.Body.Close()
		if err != nil || len(response.Data) != end-start || len(response.Error) > 0 && string(response.Error) != "null" {
			return nil, fmt.Errorf("perplexity: invalid embedding response")
		}
		batch := make([][]float64, end-start)
		for _, item := range response.Data {
			if item.Index == nil || *item.Index < 0 || *item.Index >= len(batch) || batch[*item.Index] != nil || item.Embedding == nil {
				return nil, fmt.Errorf("perplexity: invalid embedding index or value")
			}
			encoded := *item.Embedding
			if len(encoded) != base64.StdEncoding.EncodedLen(dimension) || strings.ContainsAny(encoded, "\r\n") {
				return nil, fmt.Errorf("perplexity: invalid embedding encoding or dimension")
			}
			raw, err := base64.StdEncoding.Strict().DecodeString(encoded)
			if err != nil || len(raw) != dimension {
				return nil, fmt.Errorf("perplexity: invalid embedding encoding or dimension")
			}
			vector := make([]float64, dimension)
			for i, value := range raw {
				vector[i] = float64(int8(value))
			}
			batch[*item.Index] = vector
		}
		vectors = append(vectors, batch...)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return embeddingData(vectors), nil
}

var _ ModelDriver = (*PerplexityModel)(nil)
