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

func (f batchOnlyEmbedding) Encode(_ *string, texts []string, _ *models.APIConfig, _ *models.EmbeddingConfig) ([][]float64, error) {
	return f.encode(texts)
}

func TestBoundEmbeddingBatchAndSingleQuery(t *testing.T) {
	model := models.NewEmbeddingModel(batchOnlyEmbedding{encode: func(texts []string) ([][]float64, error) {
		vectors := make([][]float64, len(texts))
		for i := range texts {
			vectors[i] = []float64{float64(i + 1), 2}
		}
		return vectors, nil
	}}, nil, nil)
	vectors, err := model.Encode([]string{"abcd", "中文"})
	if err != nil || !reflect.DeepEqual(vectors, [][]float64{{1, 2}, {2, 2}}) {
		t.Fatalf("batch=%v error=%v", vectors, err)
	}
	vectors, err = model.Encode([]string{"中文abc"})
	if err != nil || !reflect.DeepEqual(vectors, [][]float64{{1, 2}}) {
		t.Fatalf("query=%v error=%v", vectors, err)
	}
	vectors, err = model.Encode(nil)
	if err != nil || len(vectors) != 0 {
		t.Fatalf("empty=%v error=%v", vectors, err)
	}
}

func TestBoundEmbeddingRejectsInvalidResults(t *testing.T) {
	for _, vectors := range [][][]float64{nil, {{}}, {{1}, {2}}} {
		model := models.NewEmbeddingModel(batchOnlyEmbedding{encode: func([]string) ([][]float64, error) { return vectors, nil }}, nil, nil)
		if result, err := model.Encode([]string{"question"}); err == nil || result != nil {
			t.Fatalf("invalid=%v error=%v", result, err)
		}
	}
	model := models.NewEmbeddingModel(batchOnlyEmbedding{encode: func([]string) ([][]float64, error) { return [][]float64{{1}, {2, 3}}, nil }}, nil, nil)
	if result, err := model.Encode([]string{"first", "second"}); err == nil || result != nil {
		t.Fatalf("dimensions=%v error=%v", result, err)
	}
	want := errors.New("provider failure")
	model.ModelDriver = batchOnlyEmbedding{encode: func([]string) ([][]float64, error) { return nil, want }}
	if _, err := model.Encode([]string{"question"}); !errors.Is(err, want) {
		t.Fatalf("provider error=%v", err)
	}
}
