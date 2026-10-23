package nlp

import (
	"multirag/internal/entity/models"
	"sync"
	"testing"
)

type queryConfigDriver struct {
	models.ModelDriver
	t        *testing.T
	original *models.EmbeddingConfig
}

func (d *queryConfigDriver) Embed(_ *string, _ []string, _ *models.APIConfig, cfg *models.EmbeddingConfig) ([]models.EmbeddingData, error) {
	if cfg == nil || !cfg.Query || cfg.Dimension != 2 || cfg == d.original {
		d.t.Error("query configuration is not an isolated copy")
	}
	return []models.EmbeddingData{{Index: 0, Embedding: []float64{0, 1}}}, nil
}
func TestRetrievalQueryConfigIsolation(t *testing.T) {
	cfg := &models.EmbeddingConfig{Dimension: 2}
	model := models.NewEmbeddingModel(&queryConfigDriver{t: t, original: cfg}, nil, nil)
	model.EmbeddingConfig = cfg
	var wg sync.WaitGroup
	for range 16 {
		wg.Go(func() {
			if _, err := NewRetrievalService(nil).GetVector("q", model, 5, 0); err != nil {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	if cfg.Query || cfg.Dimension != 2 || model.EmbeddingConfig != cfg {
		t.Fatal("shared document configuration mutated")
	}
}
