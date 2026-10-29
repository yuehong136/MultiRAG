package models

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// BuiltinModel calls the explicitly configured TEI embedding endpoint. It has
// no provider credentials and does not accept a different model per request.
type BuiltinModel struct {
	*DummyModel
	endpoint   string
	model      string
	httpClient *http.Client
}

func NewBuiltinModel(baseURL, model string) (*BuiltinModel, error) {
	baseURL, model = strings.TrimSpace(baseURL), strings.TrimSpace(model)
	parsed, err := url.Parse(baseURL)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" || model == "" {
		return nil, fmt.Errorf("builtin requires an explicit HTTP(S) endpoint and model")
	}
	endpoint := strings.TrimRight(baseURL, "/")
	if !strings.HasSuffix(endpoint, "/embed") {
		endpoint += "/embed"
	}
	return &BuiltinModel{
		DummyModel: NewDummyModel(nil, URLSuffix{}), endpoint: endpoint, model: model,
		httpClient: &http.Client{Transport: providerModelTransport, Timeout: 30 * time.Second, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }},
	}, nil
}

func (b *BuiltinModel) Name() string { return "Builtin" }

func (b *BuiltinModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	if name == nil || *name != b.model {
		return nil, fmt.Errorf("builtin model does not match the configured TEI model")
	}
	if config != nil && config.Dimension < 0 {
		return nil, fmt.Errorf("embedding dimension must be non-negative")
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	if len(texts) == 0 {
		return []EmbeddingData{}, nil
	}
	dimension := 0
	if config != nil {
		dimension = config.Dimension
	}
	vectors := make([][]float64, 0, len(texts))
	for start := 0; start < len(texts); start += 32 {
		end := min(start+32, len(texts))
		payload := map[string]any{"inputs": texts[start:end]}
		if config != nil && config.Dimension > 0 {
			payload["dimensions"] = config.Dimension
		}
		body, err := json.Marshal(payload)
		if err != nil {
			return nil, err
		}
		req, err := http.NewRequestWithContext(requestContext(local), http.MethodPost, b.endpoint, bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		resp, err := b.httpClient.Do(req)
		if err != nil {
			return nil, fmt.Errorf("builtin embedding request: %w", err)
		}
		// Decode pointers so null cannot silently become a valid numeric zero.
		var raw [][]*float64
		decodeErr := decodeBoundedModelJSON(resp.Body, &raw)
		resp.Body.Close()
		if err := requestContext(local).Err(); err != nil {
			return nil, err
		}
		if resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("builtin embedding request failed (HTTP %d)", resp.StatusCode)
		}
		if decodeErr != nil {
			return nil, fmt.Errorf("invalid builtin embedding response")
		}
		if len(raw) != end-start {
			return nil, fmt.Errorf("builtin embedding count does not match inputs")
		}
		for _, values := range raw {
			if dimension == 0 {
				dimension = len(values)
			}
			if dimension == 0 || len(values) != dimension {
				return nil, fmt.Errorf("builtin embedding dimensions do not match")
			}
			vector := make([]float64, len(values))
			for index, value := range values {
				if value == nil {
					return nil, fmt.Errorf("builtin returned a null embedding value")
				}
				vector[index] = *value
			}
			vectors = append(vectors, vector)
		}
	}
	if err := ValidateEmbeddings(vectors, len(texts)); err != nil {
		return nil, err
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return embeddingData(vectors), nil
}

func (b *BuiltinModel) ListModels(_ *APIConfig) ([]string, error) {
	return []string{b.model}, nil
}

func (b *BuiltinModel) CheckConnection(api *APIConfig) error {
	_, err := b.Embed(&b.model, []string{"test"}, api, nil)
	return err
}

var _ ModelDriver = (*BuiltinModel)(nil)
