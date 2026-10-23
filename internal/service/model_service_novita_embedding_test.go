package service

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"testing"

	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

func TestNovitaCatalogBoundEmbeddingHTTP(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	provider := manager.FindProvider("Novita")
	if provider == nil || provider.URLSuffix.Embedding != "openai/v1/embeddings" {
		t.Fatal("Novita embedding catalog is missing")
	}
	name := "baai/bge-m3"
	found := false
	for _, model := range provider.Models {
		if model.Name == name && model.ModelTypeMap["embedding"] && model.MaxTokens == 8192 {
			found = true
		}
	}
	if !found {
		t.Fatal("Novita embedding capability is missing")
	}
	original := provider.URL["default"]
	for _, instance := range []string{"one", "two"} {
		t.Run(instance, func(t *testing.T) {
			t.Parallel()
			fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body struct {
					Model      string
					Input      []string
					Dimensions int
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if r.URL.Path != "/instance/openai/v1/embeddings" || r.Header.Get("Authorization") != "Bearer "+instance || body.Dimensions != 2 || body.Model != name || !reflect.DeepEqual(body.Input, []string{"a", "b"}) {
					t.Error("bound Novita embedding configuration lost", body, r.URL.Path)
				}
				fmt.Fprint(w, `{"data":[{"index":1,"embedding":[1,0]},{"index":0,"embedding":[0,1]}]}`)
			}))
			defer fixture.Close()
			extra, err := encodeProviderInstanceExtra("office", fixture.URL+"/instance/")
			if err != nil {
				t.Fatal(err)
			}
			driver, api, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", APIKey: instance, Extra: extra})
			if err != nil {
				t.Fatal(err)
			}
			bound := models.NewEmbeddingModel(driver, &name, api)
			bound.EmbeddingConfig = &models.EmbeddingConfig{Dimension: 2}
			data, err := bound.Embed([]string{"a", "b"})
			vectors, decodeErr := models.EmbeddingVectors(data, 2)
			if err != nil || decodeErr != nil || !reflect.DeepEqual(vectors, [][]float64{{0, 1}, {1, 0}}) {
				t.Fatal(data, err, decodeErr)
			}
			if provider.URL["default"] != original {
				t.Fatal("catalog mutated")
			}
		})
	}
}
