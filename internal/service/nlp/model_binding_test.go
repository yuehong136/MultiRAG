package nlp

import (
	"context"
	"errors"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"

	"multirag/internal/entity/models"
)

type retrievalEmbeddingDriver struct {
	models.ModelDriver
	vectors [][]float64
}

func (d *retrievalEmbeddingDriver) Encode(_ *string, _ []string, _ *models.APIConfig, _ *models.EmbeddingConfig) ([][]float64, error) {
	return d.vectors, nil
}

func TestRetrievalVectorRejectsMalformedResults(t *testing.T) {
	for _, vectors := range [][][]float64{nil, {{}}, {{1}, {2}}, {{math.Inf(1)}}} {
		model := models.NewEmbeddingModel(&retrievalEmbeddingDriver{vectors: vectors}, nil, nil)
		if expression, err := NewRetrievalService(nil).GetVector("question", model, 7, 0.4); err == nil || expression != nil {
			t.Fatalf("malformed vector=%v expression=%v error=%v", vectors, expression, err)
		}
	}
}

func TestRetrievalVectorUsesBoundDriver(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/embeddings" || r.Header.Get("Authorization") != "Bearer key" {
			t.Errorf("request=%s", r.URL.Path)
		}
		fmt.Fprint(w, `{"data":[{"index":0,"embedding":[1,2,3]}]}`)
	}))
	defer server.Close()
	name, key := "embedding", "key"
	config := &models.APIConfig{APIKey: &key}
	driver := models.NewSiliconFlowModel(map[string]string{"default": server.URL}, models.URLSuffix{Embedding: "embeddings"})
	model := models.NewEmbeddingModel(driver, &name, config)
	expression, err := NewRetrievalService(nil).GetVector("question", model, 7, 0.4)
	if err != nil {
		t.Fatal(err)
	}
	if expression.VectorColumnName != "q_3_vec" || expression.TopN != 7 || !reflect.DeepEqual(expression.EmbeddingData, []float64{1, 2, 3}) {
		t.Fatalf("expression=%#v", expression)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	config.Context = ctx
	if _, err := NewRetrievalService(nil).GetVector("question", model, 7, 0.4); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancel error=%v", err)
	}
}
