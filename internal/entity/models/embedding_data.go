package models

import (
	"encoding/json"
	"fmt"
)

// UnmarshalJSON preserves the difference between numeric zero and absent/null data.
func (d *EmbeddingData) UnmarshalJSON(data []byte) error {
	var raw struct {
		Index     *int       `json:"index"`
		Embedding []*float64 `json:"embedding"`
	}
	if err := json.Unmarshal(data, &raw); err != nil {
		return err
	}
	if raw.Index == nil || *raw.Index < 0 {
		return fmt.Errorf("invalid embedding index")
	}
	vector := make([]float64, len(raw.Embedding))
	for i, value := range raw.Embedding {
		if value == nil {
			return fmt.Errorf("null embedding value")
		}
		vector[i] = *value
	}
	if err := ValidateEmbeddings([][]float64{vector}, 1); err != nil {
		return err
	}
	*d = EmbeddingData{Index: *raw.Index, Embedding: vector}
	return nil
}

// EmbeddingVectors validates indexed results and restores input order for vector consumers.
func EmbeddingVectors(data []EmbeddingData, textCount int) ([][]float64, error) {
	if len(data) != textCount {
		return nil, fmt.Errorf("embedding count: got %d, want %d", len(data), textCount)
	}
	vectors := make([][]float64, textCount)
	for _, item := range data {
		if item.Index < 0 || item.Index >= textCount || vectors[item.Index] != nil {
			return nil, fmt.Errorf("invalid or duplicate embedding index")
		}
		// Validate empty vectors before using nil as the missing-slot marker.
		if len(item.Embedding) == 0 {
			return nil, fmt.Errorf("empty embedding")
		}
		vectors[item.Index] = item.Embedding
	}
	if err := ValidateEmbeddings(vectors, textCount); err != nil {
		return nil, err
	}
	return vectors, nil
}

func embeddingData(vectors [][]float64) []EmbeddingData {
	data := make([]EmbeddingData, len(vectors))
	for i, vector := range vectors {
		data[i] = EmbeddingData{Index: i, Embedding: vector}
	}
	return data
}
