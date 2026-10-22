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

type embeddingHTTPOptions struct {
	optionalAPIKey bool
	fallbackRegion bool
	parameters     map[string]any
}

// encodeHTTPWithConfig carries optional dimensions and provider parameters through
// every batch, retaining strict credentials and region selection by default.
func encodeHTTPWithConfig(client *http.Client, baseURLs map[string]string, suffix string, name *string, texts []string, config *APIConfig, embeddingConfig *EmbeddingConfig, options ...embeddingHTTPOptions) ([][]float64, error) {
	if len(texts) == 0 {
		return [][]float64{}, nil
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
		if embeddingConfig != nil && embeddingConfig.Dimension > 0 {
			payload["dimensions"] = embeddingConfig.Dimension
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
			req.Header.Set("Authorization", "Bearer "+key)
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
	if err := requestContext(local).Err(); err != nil {
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
