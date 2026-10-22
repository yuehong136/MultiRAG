package nlp

import "multirag/internal/entity/models"

func embeddingVectorsForTest(data []models.EmbeddingData, err error) ([][]float64, error) {
	if err != nil {
		return nil, err
	}
	return models.EmbeddingVectors(data, len(data))
}
