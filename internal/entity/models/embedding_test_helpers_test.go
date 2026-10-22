package models

func embeddingVectorsForTest(data []EmbeddingData, err error) ([][]float64, error) {
	if err != nil {
		return nil, err
	}
	return EmbeddingVectors(data, len(data))
}
