package service

import (
	"fmt"
	"net/http"
	"path/filepath"
	"reflect"
	"testing"

	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

func TestOpenAIConfiguredInstanceHTTP(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	provider := manager.FindProvider("OpenAI")
	if provider == nil || provider.ModelDriver.Name() != "openai" || provider.URLSuffix.Models != "models" {
		t.Fatal("config/factory wiring")
	}
	original := provider.URL["default"]
	for _, instance := range []string{"one", "two"} {
		t.Run(instance, func(t *testing.T) {
			t.Parallel()
			server := registeredModelServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/instance/v1/models" || r.Header.Get("Authorization") != "Bearer "+instance {
					t.Error("instance URL or key lost")
				}
				fmt.Fprintf(w, `{"data":[{"id":%q}]}`, instance)
			}))
			defer server.Close()
			extra, err := encodeProviderInstanceExtra("", server.URL+"/instance/v1/")
			if err != nil {
				t.Fatal(err)
			}
			driver, api, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", APIKey: instance, Extra: extra})
			if err != nil {
				t.Fatal(err)
			}
			ids, err := driver.ListModels(api)
			if err != nil || !reflect.DeepEqual(ids, []string{instance}) {
				t.Fatal(ids, err)
			}
			if err := driver.CheckConnection(api); err != nil {
				t.Fatal(err)
			}
			if provider.URL["default"] != original || provider.ModelDriver.(*models.OpenAIModel).BaseURL["default"] != original {
				t.Fatal("global provider mutated")
			}
			t.Log("instance discovery and CheckConnection made real controlled HTTP calls without changing catalog")
		})
	}
}
