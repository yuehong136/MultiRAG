package models

import (
	"encoding/json"

	"reflect"
	"testing"
)

type indexedEmbeddingDriver struct {
	ModelDriver
	data  []EmbeddingData
	calls int
}

func (d *indexedEmbeddingDriver) Embed(_ *string, _ []string, _ *APIConfig, _ *EmbeddingConfig) ([]EmbeddingData, error) {
	d.calls++
	return d.data, nil
}

func TestEmbeddingDataJSONRejectsAbsentAndNull(t *testing.T) {
	for _, body := range []string{`null`, `{}`, `{"index":null,"embedding":[1]}`, `{"index":0}`, `{"index":0,"embedding":[null]}`, `{"index":0,"embedding":[]}`, `{"index":0.5,"embedding":[1]}`, `{"index":0,"embedding":[1e400]}`} {
		var data EmbeddingData
		if err := json.Unmarshal([]byte(body), &data); err == nil {
			t.Fatal("malformed data accepted", body)
		}
	}
	var data EmbeddingData
	if err := json.Unmarshal([]byte(`{"index":0,"embedding":[0,1]}`), &data); err != nil || !reflect.DeepEqual(data, EmbeddingData{Index: 0, Embedding: []float64{0, 1}}) {
		t.Fatal(data, err)
	}
}
