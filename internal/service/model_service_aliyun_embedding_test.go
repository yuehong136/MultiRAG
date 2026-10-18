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

func TestAliyunEmbeddingConfiguredInstancesHTTP(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	provider := manager.FindProvider("Aliyun")
	if provider == nil || provider.URLSuffix.Embedding != "compatible-mode/v1/embeddings" {
		t.Fatal("embedding catalog endpoint missing")
	}
	original := provider.URL["default"]
	for _, name := range []string{"text-embedding-v3", "text-embedding-v4"} {
		model, err := manager.GetModelByName("Aliyun", name)
		if err != nil || model == nil || !model.ModelTypeMap["embedding"] || model.MaxTokens != 8192 {
			t.Fatal("embedding model catalog", model, err)
		}
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/"+name+"/compatible-mode/v1/embeddings" || r.Header.Get("Authorization") != "Bearer "+name {
					t.Error("instance endpoint/key lost")
				}
				var body struct {
					Model string
					Input []string
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Model != name || !reflect.DeepEqual(body.Input, []string{"first", "second"}) {
					t.Error(body, err)
				}
				fmt.Fprint(w, `{"data":[{"index":1,"embedding":[3,4]},{"index":0,"embedding":[1,2]}]}`)
			}))
			defer server.Close()
			extra, err := encodeProviderInstanceExtra("singapore", server.URL+"/"+name+"/")
			if err != nil {
				t.Fatal(err)
			}
			driver, api, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", APIKey: name, Extra: extra})
			if err != nil {
				t.Fatal(err)
			}
			bound := models.NewEmbeddingModel(driver, &name, api)
			got, err := bound.Encode([]string{"first", "second"})
			if err != nil || !reflect.DeepEqual(got, [][]float64{{1, 2}, {3, 4}}) {
				t.Fatal(got, err)
			}
			if provider.URL["default"] != original || provider.ModelDriver.(*models.AliyunModel).BaseURL["default"] != original {
				t.Fatal("shared catalog changed")
			}
			t.Logf("%s: actual regional embeddings request, per-instance key and ordered vectors verified", name)
		})
	}
}
