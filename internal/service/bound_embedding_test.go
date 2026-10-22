package service

import (
	"errors"
	"reflect"
	"testing"

	"multirag/internal/entity/models"
)

type batchOnlyEmbedding struct {
	models.ModelDriver
	encode func([]string) ([][]float64, error)
}

func (f batchOnlyEmbedding) Embed(_ *string, texts []string, _ *models.APIConfig, _ *models.EmbeddingConfig) ([]models.EmbeddingData, error) {
	vectors, err := f.encode(texts)
	if err != nil {
		return nil, err
	}
	data := make([]models.EmbeddingData, len(vectors))
	for i, v := range vectors {
		data[i] = models.EmbeddingData{Index: i, Embedding: v}
	}
	return data, nil
}

func TestBoundEmbeddingBatchAndSingleQuery(t *testing.T) {
	model := models.NewEmbeddingModel(batchOnlyEmbedding{encode: func(texts []string) ([][]float64, error) {
		vectors := make([][]float64, len(texts))
		for i := range texts {
			vectors[i] = []float64{float64(i + 1), 2}
		}
		return vectors, nil
	}}, nil, nil)
	vectors, err := embeddingVectorsForTest(model.Embed([]string{"abcd", "中文"}))
	if err != nil || !reflect.DeepEqual(vectors, [][]float64{{1, 2}, {2, 2}}) {
		t.Fatalf("batch=%v error=%v", vectors, err)
	}
	vectors, err = embeddingVectorsForTest(model.Embed([]string{"中文abc"}))
	if err != nil || !reflect.DeepEqual(vectors, [][]float64{{1, 2}}) {
		t.Fatalf("query=%v error=%v", vectors, err)
	}
	vectors, err = embeddingVectorsForTest(model.Embed(nil))
	if err != nil || len(vectors) != 0 {
		t.Fatalf("empty=%v error=%v", vectors, err)
	}
}

func TestBoundEmbeddingRejectsInvalidResults(t *testing.T) {
	for _, vectors := range [][][]float64{nil, {{}}, {{1}, {2}}} {
		model := models.NewEmbeddingModel(batchOnlyEmbedding{encode: func([]string) ([][]float64, error) { return vectors, nil }}, nil, nil)
		if result, err := embeddingVectorsForTest(model.Embed([]string{"question"})); err == nil || result != nil {
			t.Fatalf("invalid=%v error=%v", result, err)
		}
	}
	model := models.NewEmbeddingModel(batchOnlyEmbedding{encode: func([]string) ([][]float64, error) { return [][]float64{{1}, {2, 3}}, nil }}, nil, nil)
	if result, err := embeddingVectorsForTest(model.Embed([]string{"first", "second"})); err == nil || result != nil {
		t.Fatalf("dimensions=%v error=%v", result, err)
	}
	want := errors.New("provider failure")
	model.ModelDriver = batchOnlyEmbedding{encode: func([]string) ([][]float64, error) { return nil, want }}
	if _, err := embeddingVectorsForTest(model.Embed([]string{"question"})); !errors.Is(err, want) {
		t.Fatalf("provider error=%v", err)
	}
}
