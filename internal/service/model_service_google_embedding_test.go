package service

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"

	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

func TestGoogleEmbeddingCatalogConfiguredInstancesHTTP(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	provider := manager.FindProvider("Google")
	if provider == nil {
		t.Fatal("Google catalog missing")
	}
	model, err := manager.GetModelByName("Google", "text-embedding-004")
	if err != nil || model == nil || !model.ModelTypeMap["embedding"] || model.ModelTypeMap["chat"] || model.MaxTokens != 2048 {
		t.Fatal("embedding catalog", model, err)
	}
	original := provider.URL["default"]
	for _, instance := range []string{"one", "two"} {
		t.Run(instance, func(t *testing.T) {
			t.Parallel()
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.Header.Get("x-goog-api-key") != instance || r.URL.Query().Has("key") {
					t.Error("credentials crossed instances")
				}
				if r.Method == http.MethodGet {
					if r.URL.Path != "/"+instance+"/v1beta/models" {
						t.Error("discovery instance URL lost")
					}
					fmt.Fprint(w, `{"models":[{"name":"models/text-embedding-004"}]}`)
					return
				}
				if r.URL.Path != "/"+instance+"/v1beta/models/text-embedding-004:batchEmbedContents" {
					t.Error("embedding instance URL lost")
				}
				var body struct {
					Requests []struct {
						TaskType             string
						OutputDimensionality int
					}
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil || len(body.Requests) != 2 {
					t.Error("SDK batch lost", err)
					return
				}
				for _, request := range body.Requests {
					if request.TaskType != "RETRIEVAL_QUERY" || request.OutputDimensionality != 2 {
						t.Error("task/dimension lost", request)
					}
				}
				fmt.Fprint(w, `{"embeddings":[{"values":[0,1]},{"values":[2,0]}]}`)
			}))
			defer server.Close()
			extra, err := encodeProviderInstanceExtra("office", server.URL+"/"+instance)
			if err != nil {
				t.Fatal(err)
			}
			driver, api, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", APIKey: instance, Extra: extra})
			if err != nil {
				t.Fatal(err)
			}
			name := "text-embedding-004"
			bound := models.NewEmbeddingModel(driver, &name, api)
			bound.EmbeddingConfig = &models.EmbeddingConfig{Query: true, Dimension: 2}
			vectors, err := bound.Encode([]string{"a", "b"})
			if err != nil || !reflect.DeepEqual(vectors, [][]float64{{0, 1}, {2, 0}}) {
				t.Fatal(vectors, err)
			}
			if err := driver.CheckConnection(api); err != nil {
				t.Fatal(err)
			}
			if calls.Load() != 2 || api.Context != nil || !bound.EmbeddingConfig.Query || bound.EmbeddingConfig.Dimension != 2 {
				t.Fatal("request state changed")
			}
			if provider.URL["default"] != original || provider.ModelDriver.(*models.GoogleModel).BaseURL["default"] != original || !strings.HasPrefix(original, "https://") {
				t.Fatal("shared catalog changed")
			}
		})
	}
}
