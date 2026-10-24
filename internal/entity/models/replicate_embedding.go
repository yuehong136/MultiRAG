package models

import (
	"encoding/json"
	"fmt"
	"net/http"
)

// Embed supports Replicate's text/text_batch prediction schema. Model-specific
// schemas with other input fields require a separate adapter.
func (r *ReplicateModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	if len(texts) == 0 {
		return []EmbeddingData{}, nil
	}
	if name == nil {
		return nil, fmt.Errorf("replicate: model name is required")
	}
	dimension := 0
	if config != nil {
		dimension = config.Dimension
	}
	if dimension < 0 {
		return nil, fmt.Errorf("replicate: dimension must be non-negative")
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	endpoint, version, err := r.predictionEndpoint(*name, local)
	if err != nil {
		return nil, err
	}
	input := map[string]any{}
	if len(texts) == 1 {
		input["text"] = texts[0]
	} else {
		raw, err := json.Marshal(texts)
		if err != nil {
			return nil, err
		}
		input["text_batch"] = string(raw)
	}
	body := map[string]any{"input": input, "stream": false}
	if version != "" {
		body["version"] = version
	}
	prediction, err := r.prediction(endpoint, http.MethodPost, local, body, true)
	if err != nil {
		return nil, err
	}
	prediction, err = r.wait(prediction, endpoint, local)
	if err != nil {
		return nil, err
	}
	var items []json.RawMessage
	if err := json.Unmarshal(prediction.Output, &items); err != nil || len(items) != len(texts) {
		return nil, fmt.Errorf("replicate: invalid embedding count or output")
	}
	vectors := make([][]float64, len(items))
	for i, item := range items {
		raw := item
		if len(item) > 0 && item[0] == '{' {
			var object struct {
				Embedding json.RawMessage `json:"embedding"`
			}
			if err := json.Unmarshal(item, &object); err != nil {
				return nil, fmt.Errorf("replicate: invalid embedding object")
			}
			raw = object.Embedding
		}
		var values []*float64
		if err := json.Unmarshal(raw, &values); err != nil || len(values) == 0 {
			return nil, fmt.Errorf("replicate: invalid embedding vector")
		}
		if dimension == 0 {
			dimension = len(values)
		}
		if len(values) != dimension {
			return nil, fmt.Errorf("replicate: embedding dimension mismatch")
		}
		vectors[i] = make([]float64, len(values))
		for j, value := range values {
			if value == nil {
				return nil, fmt.Errorf("replicate: missing embedding component")
			}
			vectors[i][j] = *value
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
