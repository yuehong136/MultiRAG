package models

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
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
	if len(texts) == 0 {
		return [][]float64{}, nil
	}
	if name == nil || *name == "" || config == nil || config.APIKey == nil || *config.APIKey == "" {
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
	dimension := 0
	for start := 0; start < len(texts); start += 32 {
		end := min(start+32, len(texts))
		body, err := json.Marshal(map[string]interface{}{"model": *name, "input": texts[start:end]})
		if err != nil {
			return nil, err
		}
		req, err := http.NewRequestWithContext(requestContext(config), http.MethodPost, endpoint, bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+*config.APIKey)
		resp, err := client.Do(req)
		if err != nil {
			return nil, fmt.Errorf("embedding request: %w", err)
		}
		var result struct {
			Data []struct {
				Index     *int      `json:"index"`
				Embedding []float64 `json:"embedding"`
			} `json:"data"`
			Error json.RawMessage `json:"error"`
		}
		decodeErr := json.NewDecoder(resp.Body).Decode(&result)
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
			for _, value := range item.Embedding {
				if math.IsInf(value, 0) || math.IsNaN(value) {
					return nil, fmt.Errorf("invalid embedding value")
				}
			}
			batch[*item.Index] = item.Embedding
		}
		embeddings = append(embeddings, batch...)
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
