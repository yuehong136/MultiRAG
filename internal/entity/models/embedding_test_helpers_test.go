package models

import "sort"

func embeddingVectorsForTest(data []EmbeddingData, err error) ([][]float64, error) {
	if err != nil {
		return nil, err
	}
	rows := append([]EmbeddingData(nil), data...)
	sort.SliceStable(rows, func(i, j int) bool { return rows[i].Index < rows[j].Index })
	vectors := make([][]float64, len(rows))
	for i, row := range rows {
		vectors[i] = row.Embedding
	}
	return vectors, nil
}

func indexedVectorsForTest(vectors [][]float64) []EmbeddingData {
	data := make([]EmbeddingData, len(vectors))
	for i, vector := range vectors {
		data[i] = EmbeddingData{Index: i, Embedding: vector}
	}
	return data
}
