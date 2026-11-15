package service

import (
	"fmt"
	"math"
	"multirag/internal/entity/models"
)

func embeddingVectorsForTest(data []models.EmbeddingData, err error) ([][]float64, error) {
	if err != nil {
		return nil, err
	}
	out := make([][]float64, len(data))
	for _, row := range data {
		if row.Index < 0 || row.Index >= len(data) || out[row.Index] != nil || len(row.Embedding) == 0 {
			return nil, fmt.Errorf("invalid indexed embedding")
		}
		for _, value := range row.Embedding {
			if math.IsNaN(value) || math.IsInf(value, 0) {
				return nil, fmt.Errorf("invalid embedding value")
			}
		}
		out[row.Index] = row.Embedding
	}
	return out, nil
}
