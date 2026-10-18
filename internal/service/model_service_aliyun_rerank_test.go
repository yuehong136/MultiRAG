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

func TestAliyunRerankConfiguredInstancesHTTP(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	provider := manager.FindProvider("Aliyun")
	model, err := manager.GetModelByName("Aliyun", "qwen3-rerank")
	if provider == nil || provider.URLSuffix.Rerank != "compatible-api/v1/reranks" || err != nil || !model.ModelTypeMap["rerank"] || model.MaxTokens != 8192 {
		t.Fatal("existing catalog contract", model, err)
	}
	original := provider.URL["default"]
	for _, instance := range []string{"one", "two"} {
		t.Run(instance, func(t *testing.T) {
			t.Parallel()
			score := 0.3
			if instance == "two" {
				score = 0.9
			}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/"+instance+"/compatible-api/v1/reranks" || r.Header.Get("Authorization") != "Bearer "+instance {
					t.Error("instance URL/key lost")
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if body["model"] != "qwen3-rerank" || body["top_n"] != float64(2) || body["return_documents"] != false {
					t.Error("rerank payload lost")
				}
				fmt.Fprintf(w, `{"results":[{"index":1,"relevance_score":%g},{"index":0,"relevance_score":0}]}`, score)
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
			name := "qwen3-rerank"
			bound := models.NewRerankModel(driver, &name, api)
			scores, err := bound.Rerank("q", []string{"zero", "one"})
			if err != nil || !reflect.DeepEqual(scores, []float64{0, score}) {
				t.Fatal(scores, err)
			}
			if provider.URL["default"] != original || provider.ModelDriver.(*models.AliyunModel).BaseURL["default"] != original {
				t.Fatal("shared provider URL mutated")
			}
			t.Logf("instance %s: actual POST rerank returned ordered scores %v through bound driver; catalog unchanged", instance, scores)
		})
	}
}
