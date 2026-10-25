package models

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"maps"
	"math"
	"net/http"
	"strings"
	"time"
)

// ValidateEmbeddings checks the batch contract before consumers index vectors or use their dimension.
func ValidateEmbeddings(embeddings [][]float64, textCount int) error {
	if len(embeddings) != textCount {
		return fmt.Errorf("embedding count: got %d, want %d", len(embeddings), textCount)
	}
	dimension := 0
	for _, vector := range embeddings {
		if len(vector) == 0 {
			return fmt.Errorf("empty embedding")
		}
		if dimension == 0 {
			dimension = len(vector)
		}
		if len(vector) != dimension {
			return fmt.Errorf("inconsistent embedding dimensions")
		}
		for _, value := range vector {
			if math.IsInf(value, 0) || math.IsNaN(value) {
				return fmt.Errorf("invalid embedding value")
			}
		}
	}
	return nil
}

func requestContext(config *APIConfig) context.Context {
	if config != nil && config.Context != nil {
		return config.Context
	}
	return context.Background()
}

type embeddingHTTPOptions struct {
	optionalAPIKey    bool
	rejectNonzeroCode bool
	fallbackRegion    bool
	parameters        map[string]any
	dimensionField    string
	authHeader        string
	omitModel         bool
}

// embedHTTPWithConfig carries optional dimensions and provider parameters through
// every batch, retaining strict credentials and region selection by default.
func embedHTTPWithConfig(client *http.Client, baseURLs map[string]string, suffix string, name *string, texts []string, config *APIConfig, embeddingConfig *EmbeddingConfig, options ...embeddingHTTPOptions) ([]EmbeddingData, error) {
	if len(texts) == 0 {
		return []EmbeddingData{}, nil
	}
	option := embeddingHTTPOptions{}
	if len(options) > 0 {
		option = options[0]
	}
	dimension := 0
	if embeddingConfig != nil {
		if embeddingConfig.Dimension < 0 {
			return nil, fmt.Errorf("embedding dimension must be non-negative")
		}
		dimension = embeddingConfig.Dimension
	}
	if name == nil || strings.TrimSpace(*name) == "" {
		return nil, fmt.Errorf("embedding model name is required")
	}
	key := ""
	if config != nil && config.APIKey != nil {
		key = strings.TrimSpace(*config.APIKey)
	}
	if key == "" && !option.optionalAPIKey {
		return nil, fmt.Errorf("embedding API key is required")
	}
	if suffix == "" {
		return nil, fmt.Errorf("embedding endpoint is not configured")
	}
	local, cancel := modelCallConfig(config)
	defer cancel()
	if option.fallbackRegion && local.Region != nil && strings.TrimRight(baseURLs[*local.Region], "/") == "" {
		local.Region = nil
	}
	baseURL, err := resolveModelBaseURL(baseURLs, local.Region)
	if err != nil {
		return nil, err
	}
	endpoint := joinModelURL(baseURL, suffix)
	if strings.HasSuffix(baseURL, "/"+strings.TrimLeft(suffix, "/")) {
		endpoint = baseURL
	}
	embeddings := make([][]float64, 0, len(texts))
	for start := 0; start < len(texts); start += 32 {
		end := min(start+32, len(texts))
		payload := make(map[string]any, len(option.parameters)+3)
		for field, value := range option.parameters {
			payload[field] = value
		}
		payload["model"], payload["input"] = *name, texts[start:end]
		if option.omitModel {
			delete(payload, "model")
		}
		if embeddingConfig != nil && embeddingConfig.Dimension > 0 {
			field := option.dimensionField
			if field == "" {
				field = "dimensions"
			}
			payload[field] = embeddingConfig.Dimension
		}
		body, err := json.Marshal(payload)
		if err != nil {
			return nil, err
		}
		req, err := http.NewRequestWithContext(requestContext(local), http.MethodPost, endpoint, bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		if key != "" {
			if option.authHeader != "" {
				req.Header.Set(option.authHeader, key)
			} else {
				req.Header.Set("Authorization", "Bearer "+key)
			}
		}
		resp, err := client.Do(req)
		if err != nil {
			return nil, fmt.Errorf("embedding request: %w", err)
		}
		var result struct {
			Data []struct {
				Index     *int       `json:"index"`
				Embedding []*float64 `json:"embedding"`
			} `json:"data"`
			Error json.RawMessage `json:"error"`
			Code  json.RawMessage `json:"code"`
		}
		decodeErr := decodeBoundedModelJSON(resp.Body, &result)
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("embedding request failed (HTTP %d)", resp.StatusCode)
		}
		if decodeErr != nil {
			return nil, fmt.Errorf("decode embeddings: %w", decodeErr)
		}
		if len(result.Error) != 0 && string(result.Error) != "null" {
			return nil, fmt.Errorf("embedding provider returned an error")
		}
		if option.rejectNonzeroCode {
			if err := validateModelResponseCode(result.Code); err != nil {
				return nil, err
			}
		}
		size := end - start
		if len(result.Data) != size {
			return nil, fmt.Errorf("embedding count: got %d, want %d", len(result.Data), size)
		}
		batch := make([][]float64, size)
		for _, item := range result.Data {
			if item.Index == nil || *item.Index < 0 || *item.Index >= size || batch[*item.Index] != nil {
				return nil, fmt.Errorf("invalid or duplicate embedding index")
			}
			if len(item.Embedding) == 0 {
				return nil, fmt.Errorf("empty embedding")
			}
			if dimension == 0 {
				dimension = len(item.Embedding)
			}
			if len(item.Embedding) != dimension {
				return nil, fmt.Errorf("inconsistent embedding dimensions")
			}
			vector := make([]float64, len(item.Embedding))
			for i, value := range item.Embedding {
				if value == nil || math.IsInf(*value, 0) || math.IsNaN(*value) {
					return nil, fmt.Errorf("invalid embedding value")
				}
				vector[i] = *value
			}
			batch[*item.Index] = vector
		}
		embeddings = append(embeddings, batch...)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return embeddingData(embeddings), nil
}

// OpenAIEmbeddingDriver retains the legacy embedding capability without adding
// chat support to providers whose Go chat implementation is still unavailable.
type OpenAIEmbeddingDriver struct {
	*DummyModel
	httpClient *http.Client
}

func NewOpenAIEmbeddingDriver(baseURLs map[string]string, suffix URLSuffix) *OpenAIEmbeddingDriver {
	if suffix.Embedding == "" {
		suffix.Embedding = "embeddings"
	}
	return &OpenAIEmbeddingDriver{DummyModel: NewDummyModel(maps.Clone(baseURLs), suffix), httpClient: &http.Client{Timeout: 120 * time.Second}}
}
func (m *OpenAIEmbeddingDriver) Name() string { return "openai-compatible" }
func (m *OpenAIEmbeddingDriver) Embed(name *string, texts []string, config *APIConfig, embeddingConfig *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(m.httpClient, m.BaseURL, m.URLSuffix.Embedding, name, texts, config, embeddingConfig)
}
