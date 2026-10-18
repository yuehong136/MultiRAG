package models

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
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

// encodeHTTP preserves the old OpenAI-compatible embedding path, with ordered
// batch validation. SiliconFlow limits one request to 32 inputs.
func encodeHTTP(client *http.Client, baseURLs map[string]string, suffix string, name *string, texts []string, config *APIConfig) ([][]float64, error) {
	return encodeHTTPWithConfig(client, baseURLs, suffix, name, texts, config, nil)
}

// encodeHTTPWithConfig carries optional dimensions through every batch and
// validates the provider's vectors against the requested dimension.
func encodeHTTPWithConfig(client *http.Client, baseURLs map[string]string, suffix string, name *string, texts []string, config *APIConfig, embeddingConfig *EmbeddingConfig) ([][]float64, error) {
	if len(texts) == 0 {
		return [][]float64{}, nil
	}
	dimension := 0
	if embeddingConfig != nil {
		if embeddingConfig.Dimension < 0 {
			return nil, fmt.Errorf("embedding dimension must be non-negative")
		}
		dimension = embeddingConfig.Dimension
	}
	if name == nil || strings.TrimSpace(*name) == "" || config == nil || config.APIKey == nil || strings.TrimSpace(*config.APIKey) == "" {
		return nil, fmt.Errorf("embedding model name and API key are required")
	}
	if suffix == "" {
		return nil, fmt.Errorf("embedding endpoint is not configured")
	}
	baseURL, err := resolveModelBaseURL(baseURLs, config.Region)
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
		payload := map[string]interface{}{"model": *name, "input": texts[start:end]}
		if embeddingConfig != nil && embeddingConfig.Dimension > 0 {
			payload["dimensions"] = embeddingConfig.Dimension
		}
		body, err := json.Marshal(payload)
		if err != nil {
			return nil, err
		}
		req, err := http.NewRequestWithContext(requestContext(config), http.MethodPost, endpoint, bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*config.APIKey))
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
		}
		decoder := json.NewDecoder(resp.Body)
		decodeErr := decoder.Decode(&result)
		if decodeErr == nil {
			var trailing any
			if err := decoder.Decode(&trailing); err != io.EOF {
				decodeErr = fmt.Errorf("embedding response has trailing content")
			}
		}
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
	if err := requestContext(config).Err(); err != nil {
		return nil, err
	}
	return embeddings, nil
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
	return &OpenAIEmbeddingDriver{DummyModel: NewDummyModel(baseURLs, suffix), httpClient: &http.Client{Timeout: 120 * time.Second}}
}
func (m *OpenAIEmbeddingDriver) Name() string { return "openai-compatible" }
func (m *OpenAIEmbeddingDriver) Encode(name *string, texts []string, config *APIConfig, embeddingConfig *EmbeddingConfig) ([][]float64, error) {
	return encodeHTTP(m.httpClient, m.BaseURL, m.URLSuffix.Embedding, name, texts, config)
}
