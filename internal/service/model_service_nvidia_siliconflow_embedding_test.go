package service

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"strconv"
	"sync/atomic"
	"testing"

	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

func TestNVIDIAEmbeddingCatalog(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	provider := manager.FindProvider("NVIDIA")
	if provider == nil || provider.URLSuffix.Embedding != "embeddings" || len(provider.Models) != 62 {
		t.Fatal("NVIDIA embedding catalog missing")
	}
	for name, tokens := range map[string]int{
		"baai/bge-m3":                       8192,
		"nvidia/llama-3.2-nv-embedqa-1b-v2": 8192,
		"nvidia/nv-embed-v1":                32768,
		"nvidia/nv-embedqa-e5-v5":           512,
		"nvidia/nv-embedqa-mistral-7b-v2":   512,
		"snowflake/arctic-embed-l":          512,
	} {
		model, err := manager.GetModelByName("NVIDIA", name)
		if err != nil || model == nil || !model.ModelTypeMap["embedding"] || model.MaxTokens != tokens {
			t.Fatal("NVIDIA embedding model metadata", name, model, err)
		}
	}
	if model, err := manager.GetModelByName("NVIDIA", "nvidia/llama-3.2-nemoretriever-1b-vlm-embed-v1"); err != nil || !model.ModelTypeMap["embedding"] {
		t.Fatal("existing embedding model lost", model, err)
	}
}

func TestNVIDIASiliconFlowEmbeddingConfiguredInstancesHTTP(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	for _, providerName := range []string{"NVIDIA", "SiliconFlow"} {
		provider := manager.FindProvider(providerName)
		if provider == nil || provider.URLSuffix.Embedding != "embeddings" {
			t.Fatal("missing embedding provider", providerName)
		}
		original := provider.URL["default"]
		for _, instance := range []string{"one", "two"} {
			t.Run(providerName+"/"+instance, func(t *testing.T) {
				t.Parallel()
				name := "baai/bge-m3"
				if providerName == "SiliconFlow" {
					name = "Qwen/Qwen3-Embedding-0.6B"
				}
				var calls atomic.Int32
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					calls.Add(1)
					var body struct {
						Model          string
						Input          []string
						Dimensions     int
						InputType      string `json:"input_type"`
						EncodingFormat string `json:"encoding_format"`
						Truncate       string
					}
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
					}
					if r.URL.Path != "/"+instance+"/v1/embeddings" || r.Header.Get("Authorization") != "Bearer "+instance || body.Model != name || body.Dimensions != 2 || len(body.Input) > 32 {
						t.Error("instance binding or embedding configuration lost", body, r.URL.Path)
					}
					if providerName == "NVIDIA" && (body.InputType != "query" || body.EncodingFormat != "float" || body.Truncate != "END") {
						t.Error("NVIDIA provider parameters lost", body)
					}
					data := make([]map[string]any, 0, len(body.Input))
					for i := len(body.Input) - 1; i >= 0; i-- {
						value, err := strconv.Atoi(body.Input[i])
						if err != nil {
							t.Error(err)
						}
						data = append(data, map[string]any{"index": i, "embedding": []float64{float64(value), 0}})
					}
					json.NewEncoder(w).Encode(map[string]any{"object": "list", "data": data})
				}))
				defer server.Close()
				extra, err := encodeProviderInstanceExtra("office", server.URL+"/"+instance+"/v1/")
				if err != nil {
					t.Fatal(err)
				}
				driver, api, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", APIKey: instance, Extra: extra})
				if err != nil {
					t.Fatal(err)
				}
				bound := models.NewEmbeddingModel(driver, &name, api)
				bound.EmbeddingConfig = &models.EmbeddingConfig{Query: true, Dimension: 2}
				texts, want := make([]string, 33), make([][]float64, 33)
				for i := range texts {
					texts[i], want[i] = strconv.Itoa(i), []float64{float64(i), 0}
				}
				vectors, err := embeddingVectorsForTest(bound.Embed(texts))
				if err != nil || !reflect.DeepEqual(vectors, want) || calls.Load() != 2 || api.Context != nil || !bound.EmbeddingConfig.Query || bound.EmbeddingConfig.Dimension != 2 || provider.URL["default"] != original {
					t.Fatal("bound embedding failed or mutated its configuration", vectors, err, calls.Load())
				}
			})
		}
	}
}
