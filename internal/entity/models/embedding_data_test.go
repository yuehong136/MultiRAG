package models

import (
	"context"
	"encoding/json"
	"errors"
	"math"
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

func TestBoundIndexedEmbeddings(t *testing.T) {
	for _, tc := range []struct {
		name  string
		data  []EmbeddingData
		valid bool
	}{
		{"reordered zero components", []EmbeddingData{{1, []float64{1, 0}}, {0, []float64{0, 2}}}, true},
		{"duplicate", []EmbeddingData{{0, []float64{1}}, {0, []float64{2}}}, false},
		{"out of range", []EmbeddingData{{0, []float64{1}}, {2, []float64{2}}}, false},
		{"negative", []EmbeddingData{{-1, []float64{1}}, {1, []float64{2}}}, false},
		{"missing", []EmbeddingData{{0, []float64{1}}}, false},
		{"empty", []EmbeddingData{{0, nil}, {1, []float64{2}}}, false},
		{"nonfinite", []EmbeddingData{{0, []float64{math.NaN()}}, {1, []float64{2}}}, false},
		{"dimension", []EmbeddingData{{0, []float64{1}}, {1, []float64{2, 3}}}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			driver := &indexedEmbeddingDriver{data: tc.data}
			bound := NewEmbeddingModel(driver, nil, nil)
			data, err := bound.Embed([]string{"a", "b"})
			if (err == nil) != tc.valid || !tc.valid && data != nil {
				t.Fatal(data, err)
			}
			if tc.valid && !reflect.DeepEqual(data, []EmbeddingData{{0, []float64{0, 2}}, {1, []float64{1, 0}}}) {
				t.Fatal("global input identity lost", data)
			}
		})
	}
	driver := &indexedEmbeddingDriver{}
	bound := NewEmbeddingModel(driver, nil, nil)
	bound.EmbeddingConfig = &EmbeddingConfig{Dimension: -1}
	if _, err := bound.Embed([]string{"a"}); err == nil || driver.calls != 0 {
		t.Fatal("invalid dimension reached provider")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	bound.EmbeddingConfig = nil
	bound.APIConfig = &APIConfig{Context: ctx}
	if _, err := bound.Embed([]string{"a"}); !errors.Is(err, context.Canceled) || driver.calls != 0 {
		t.Fatal("cancelled request reached provider", err)
	}
}

func TestEmbeddingDataJSONRejectsAbsentAndNull(t *testing.T) {
	for _, body := range []string{`null`, `{}`, `{"index":null,"embedding":[1]}`, `{"index":0}`, `{"index":0,"embedding":[null]}`, `{"index":0,"embedding":[]}`, `{"index":0.5,"embedding":[1]}`, `{"index":0,"embedding":[1e400]}`} {
		var data EmbeddingData
		if err := json.Unmarshal([]byte(body), &data); err == nil {
			t.Fatal("malformed data accepted", body)
		}
	}
	var data EmbeddingData
	if err := json.Unmarshal([]byte(`{"index":0,"embedding":[0,1]}`), &data); err != nil || !reflect.DeepEqual(data, EmbeddingData{0, []float64{0, 1}}) {
		t.Fatal(data, err)
	}
}
