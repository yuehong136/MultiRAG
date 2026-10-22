package models

import (
	"encoding/json"
	"math"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sync/atomic"
	"testing"
)

type embeddingContractDriver struct {
	ModelDriver
	vectors [][]float64
	config  *EmbeddingConfig
}

func (d *embeddingContractDriver) Embed(_ *string, _ []string, _ *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	d.config = config
	return embeddingData(d.vectors), nil
}

func TestBoundEmbedValidatesBatchAndForwardsConfig(t *testing.T) {
	for _, test := range []struct {
		name    string
		texts   []string
		vectors [][]float64
		valid   bool
	}{
		{"empty input", nil, nil, true},
		{"valid batch", []string{"a", "b"}, [][]float64{{1, 2}, {3, 4}}, true},
		{"empty result", []string{"a"}, nil, false},
		{"empty vector", []string{"a"}, [][]float64{{}}, false},
		{"extra result", []string{"a"}, [][]float64{{1}, {2}}, false},
		{"missing result", []string{"a", "b"}, [][]float64{{1}}, false},
		{"inconsistent dimension", []string{"a", "b"}, [][]float64{{1}, {2, 3}}, false},
		{"nonfinite vector", []string{"a"}, [][]float64{{math.Inf(1)}}, false},
		{"NaN vector", []string{"a"}, [][]float64{{math.NaN()}}, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			driver := &embeddingContractDriver{vectors: test.vectors}
			model := NewEmbeddingModel(driver, nil, nil)
			model.EmbeddingConfig = &EmbeddingConfig{}
			vectors, err := embeddingVectorsForTest(model.Embed(test.texts))
			if (err == nil) != test.valid || !test.valid && vectors != nil {
				t.Fatalf("vectors=%v error=%v", vectors, err)
			}
			if driver.config != model.EmbeddingConfig {
				t.Fatal("embedding configuration was not forwarded")
			}
		})
	}
}

func TestEmbeddingProvidersUseCanonicalEmbed(t *testing.T) {
	for _, provider := range []string{"Aliyun", "OpenAI", "OpenAI-API-Compatible", "DeepSeek", "Moonshot", "Gitee", "SiliconFlow", "ZHIPU-AI"} {
		t.Run(provider, func(t *testing.T) {
			var requests atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				requests.Add(1)
				if r.Method != http.MethodPost || r.URL.Path != "/embeddings" || r.Header.Get("Authorization") != "Bearer fixture-key" {
					t.Errorf("request = %s %s, incorrect endpoint or credential", r.Method, r.URL.Path)
				}
				var body struct {
					Model string
					Input json.RawMessage
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Model != "resolved-model" {
					t.Errorf("model = %q, %v", body.Model, err)
				}
				var texts []string
				if err := json.Unmarshal(body.Input, &texts); err != nil {
					t.Error(err)
				}

				data := make([]map[string]interface{}, 0, len(texts))
				for i := len(texts) - 1; i >= 0; i-- {
					value := 1.0
					if texts[i] == "second" {
						value = 2
					}
					data = append(data, map[string]interface{}{"index": i, "embedding": []float64{value, 9}})
				}
				if err := json.NewEncoder(w).Encode(map[string]interface{}{"data": data}); err != nil {
					t.Error(err)
				}
			}))
			defer server.Close()
			driver, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings"})
			if err != nil {
				t.Fatal(err)
			}
			name, key := "resolved-model", "fixture-key"
			config := &APIConfig{APIKey: &key}
			if vectors, err := embeddingVectorsForTest(driver.Embed(&name, nil, config, nil)); err != nil || len(vectors) != 0 || requests.Load() != 0 {
				t.Fatalf("empty input = %v, %v; requests=%d", vectors, err, requests.Load())
			}
			vectors, err := embeddingVectorsForTest(driver.Embed(&name, []string{"first", "second"}, config, &EmbeddingConfig{}))
			if err != nil || !reflect.DeepEqual(vectors, [][]float64{{1, 9}, {2, 9}}) {
				t.Fatalf("vectors=%v error=%v", vectors, err)
			}
		})
	}
	for _, provider := range []string{"Google", "MiniMax", "xAI", "unknown"} {
		t.Run(provider, func(t *testing.T) {
			driver, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": "https://example.invalid"}, URLSuffix{})
			if err != nil {
				t.Fatal(err)
			}
			name, key := "model", "fixture-key"
			if _, err := embeddingVectorsForTest(driver.Embed(&name, []string{"text"}, &APIConfig{APIKey: &key}, nil)); err == nil {
				t.Fatalf("%s fabricated embedding support", provider)
			}
		})
	}
}

func TestZhipuEmbedRejectsEmptyAndInconsistentDimensions(t *testing.T) {
	for _, body := range []string{`{"data":[{"index":0,"embedding":[]},{"index":1,"embedding":[1]}]}`, `{"data":[{"index":0,"embedding":[1,2]},{"index":1,"embedding":[1]}]}`} {
		t.Run(body, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte(body)) }))
			defer server.Close()
			driver := NewZhipuAIModel(map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings"})
			name, key := "model", "fixture-key"
			if data, err := driver.Embed(&name, []string{"a", "b"}, &APIConfig{APIKey: &key}, nil); err == nil || data != nil {
				t.Fatal(data, err)
			}
		})
	}
}

func TestSiliconFlowEmbedValidatesDimensionsAcrossBatches(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct{ Input []string }
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		data := make([]map[string]interface{}, len(body.Input))
		for i := range body.Input {
			vector := []float64{1, 2}
			if len(body.Input) == 1 {
				vector = []float64{1}
			}
			data[i] = map[string]interface{}{"index": i, "embedding": vector}
		}
		json.NewEncoder(w).Encode(map[string]interface{}{"data": data})
	}))
	defer server.Close()
	driver := NewSiliconFlowModel(map[string]string{"default": server.URL}, URLSuffix{Embedding: "embeddings"})
	name, key := "model", "fixture-key"
	if vectors, err := embeddingVectorsForTest(driver.Embed(&name, make([]string, 33), &APIConfig{APIKey: &key}, nil)); err == nil || vectors != nil {
		t.Fatalf("mixed batch dimensions=%v error=%v", vectors, err)
	}
}
