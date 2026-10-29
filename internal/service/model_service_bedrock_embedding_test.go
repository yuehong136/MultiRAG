package service

import (
	"encoding/json"
	"fmt"
	"net/http"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

func TestBedrockEmbeddingCatalogConfiguredInstanceHTTP(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	provider := manager.FindProvider("Bedrock")
	if provider == nil || provider.URLSuffix.Embedding != "invoke" {
		t.Fatal("missing Bedrock embedding endpoint")
	}
	for name, tokens := range map[string]int{"amazon.titan-embed-text-v1": 8192, "amazon.titan-embed-text-v2:0": 8192, "cohere.embed-english-v3": 512, "cohere.embed-multilingual-v3": 512, "cohere.embed-v4:0": 128000} {
		model, err := manager.GetModelByName("Bedrock", name)
		if err != nil || model == nil || !model.ModelTypeMap["embedding"] || model.ModelTypeMap["chat"] || model.MaxTokens != tokens {
			t.Fatal("invalid embedding catalog", name, model, err)
		}
	}
	for _, instance := range []string{"one", "two"} {
		t.Run(instance, func(t *testing.T) {
			t.Parallel()
			server := registeredModelServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/"+instance+"/model/amazon.titan-embed-text-v2:0/invoke" || !strings.Contains(r.Header.Get("Authorization"), "Credential=KEY"+instance+"/") || !strings.Contains(r.Header.Get("Authorization"), "/eu-west-1/bedrock/aws4_request") {
					t.Error("instance credentials, endpoint or region lost")
				}
				var body struct {
					Text string `json:"inputText"`
				}
				if json.NewDecoder(r.Body).Decode(&body) != nil || body.Text != "text" {
					t.Error("input lost")
				}
				fmt.Fprint(w, `{"embedding":[0,1]}`)
			}))
			extra, err := encodeProviderInstanceExtra("eu-west-1", server.URL+"/"+instance)
			if err != nil {
				t.Fatal(err)
			}
			key := `{"auth_mode":"access_key_secret","bedrock_ak":"KEY` + instance + `","bedrock_sk":"controlled-secret","bedrock_region":"us-east-1"}`
			driver, api, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", APIKey: key, Extra: extra})
			if err != nil {
				t.Fatal(err)
			}
			name := "amazon.titan-embed-text-v2:0"
			data, err := models.NewEmbeddingModel(driver, &name, api).Embed([]string{"text"})
			if err != nil || !reflect.DeepEqual(data, []models.EmbeddingData{{Index: 0, Embedding: []float64{0, 1}}}) {
				t.Fatal(data, err)
			}
			if len(provider.URL) != 0 || len(provider.ModelDriver.(*models.BedrockModel).BaseURL) != 0 || api.Context != nil {
				t.Fatal("catalog or caller state changed")
			}
		})
	}
}
