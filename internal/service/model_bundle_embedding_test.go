package service

import (
	"errors"
	"reflect"
	"testing"

	"multirag/internal/entity"
)

type batchOnlyEmbedding func([]string) ([][]float64, error)

func (f batchOnlyEmbedding) Encode(texts []string) ([][]float64, error) { return f(texts) }

func TestModelBundleEmbeddingCompatibilityAndTokenEstimate(t *testing.T) {
	bundle := &ModelBundle{modelType: entity.ModelTypeEmbedding, model: batchOnlyEmbedding(func(texts []string) ([][]float64, error) {
		vectors := make([][]float64, len(texts))
		for i := range texts {
			vectors[i] = []float64{float64(i + 1), 2}
		}
		return vectors, nil
	})}
	vectors, tokens, err := bundle.Encode([]string{"abcd", "中文"})
	if err != nil || tokens != 2 || !reflect.DeepEqual(vectors, [][]float64{{1, 2}, {2, 2}}) {
		t.Fatalf("batch=%v tokens=%d error=%v", vectors, tokens, err)
	}
	vector, tokens, err := bundle.EncodeQuery("中文abc")
	if err != nil || tokens != 2 || !reflect.DeepEqual(vector, []float64{1, 2}) {
		t.Fatalf("query=%v tokens=%d error=%v", vector, tokens, err)
	}
	vectors, tokens, err = bundle.Encode(nil)
	if err != nil || tokens != 0 || len(vectors) != 0 {
		t.Fatalf("empty input=%v tokens=%d error=%v", vectors, tokens, err)
	}
}

func TestModelBundleRejectsInvalidEmbeddingResults(t *testing.T) {
	for _, vectors := range [][][]float64{nil, {{}}, {{1}, {2}}} {
		bundle := &ModelBundle{modelType: entity.ModelTypeEmbedding, model: batchOnlyEmbedding(func([]string) ([][]float64, error) { return vectors, nil })}
		if vector, tokens, err := bundle.EncodeQuery("question"); err == nil || vector != nil || tokens != 0 {
			t.Fatalf("invalid query=%v tokens=%d error=%v", vector, tokens, err)
		}
	}
	bundle := &ModelBundle{modelType: entity.ModelTypeEmbedding, model: batchOnlyEmbedding(func([]string) ([][]float64, error) { return [][]float64{{1}, {2, 3}}, nil })}
	if vectors, tokens, err := bundle.Encode([]string{"first", "second"}); err == nil || vectors != nil || tokens != 0 {
		t.Fatalf("inconsistent dimensions=%v tokens=%d error=%v", vectors, tokens, err)
	}
	want := errors.New("provider failure")
	bundle.model = batchOnlyEmbedding(func([]string) ([][]float64, error) { return nil, want })
	if _, tokens, err := bundle.EncodeQuery("question"); !errors.Is(err, want) || tokens != 0 {
		t.Fatalf("provider error=%v tokens=%d", err, tokens)
	}
	bundle.modelType = entity.ModelTypeChat
	if _, tokens, err := bundle.EncodeQuery("question"); err == nil || tokens != 0 {
		t.Fatal("wrong model type accepted")
	}
}
