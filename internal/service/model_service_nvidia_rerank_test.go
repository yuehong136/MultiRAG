package service

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"testing"

	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

func TestNVIDIARerankConfiguredInstancesHTTP(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	provider := manager.FindProvider("NVIDIA")
	for _, name := range []string{"nvidia/nv-rerankqa-mistral-4b-v3", "nvidia/llama-3.2-nv-rerankqa-1b-v2"} {
		model, err := manager.GetModelByName("NVIDIA", name)
		if err != nil || !model.ModelTypeMap["rerank"] || model.MaxTokens != 4096 || provider.URLSuffix.Rerank != "ranking" {
			t.Fatal(model, err)
		}
	}
	original := provider.URL["default"]
	for i, instance := range []string{"one", "two"} {
		t.Run(instance, func(t *testing.T) {
			t.Parallel()
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/"+instance+"/ranking" || r.Header.Get("Authorization") != "Bearer "+instance {
					t.Error("instance crossed URL/key")
				}
				fmt.Fprintf(w, `{"rankings":[{"index":1,"logit":%d},{"index":0,"logit":0}]}`, i+1)
			}))
			defer server.Close()
			extra, err := encodeProviderInstanceExtra("office", server.URL+"/"+instance+"/")
			if err != nil {
				t.Fatal(err)
			}
			driver, api, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", APIKey: instance, Extra: extra})
			if err != nil {
				t.Fatal(err)
			}
			name := "nvidia/nv-rerankqa-mistral-4b-v3"
			scores, err := models.NewRerankModel(driver, &name, api).Rerank("q", []string{"a", "b"})
			if err != nil || !reflect.DeepEqual(scores, []float64{0, float64(i + 1)}) {
				t.Fatal(scores, err)
			}
			if provider.URL["default"] != original || provider.ModelDriver.(*models.NVIDIAModel).BaseURL["default"] != original {
				t.Fatal("catalog mutated")
			}
		})
	}
}
