package service

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sync"
	"testing"

	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

func TestLocalConnectionInstanceIsolationHTTP(t *testing.T) {
	for _, name := range []string{"ollama", "LM-Studio", "vllm"} {
		t.Run(name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var id, key string
				switch r.URL.Path {
				case "/catalog/models":
					id = "catalog"
				case "/one/models":
					id = "one"
					key = "one-key"
				case "/two/models":
					id = "two"
					key = "two-key"
				default:
					t.Error("wrong instance address", r.URL.Path)
					w.WriteHeader(404)
					return
				}
				if key != "" {
					key = "Bearer " + key
				}
				if r.Header.Get("Authorization") != key {
					t.Error("credentials crossed instances")
				}
				fmt.Fprintf(w, `{"data":[{"id":%q}],"models":[{"name":%q}]}`, id, id)
			}))
			defer server.Close()
			urls := map[string]string{"default": server.URL + "/catalog"}
			catalog, _ := models.NewModelFactory().CreateModelDriver(name, urls, models.URLSuffix{Models: "models"})
			provider := &entity.Provider{Name: name, URL: urls, URLSuffix: models.URLSuffix{Models: "models"}, ModelDriver: catalog}
			var group sync.WaitGroup
			for _, id := range []string{"catalog", "one", "two"} {
				group.Go(func() {
					base, key := "", ""
					if id != "catalog" {
						base = server.URL + "/" + id
						key = id + "-key"
					}
					raw, err := encodeProviderInstanceExtra("", base)
					if err != nil {
						t.Error(err)
						return
					}
					driver, config, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", APIKey: key, Extra: raw})
					if err != nil {
						t.Error(err)
						return
					}
					for range 4 {
						if err := driver.CheckConnection(config); err != nil {
							t.Error(err)
							return
						}
						if got, err := driver.ListModels(config); err != nil || !reflect.DeepEqual(got, []string{id}) {
							t.Error(got, err)
							return
						}
					}
				})
			}
			group.Wait()
			if provider.ModelDriver != catalog || !reflect.DeepEqual(provider.URL, map[string]string{"default": server.URL + "/catalog"}) {
				t.Fatal("provider global state changed")
			}
			if _, err := catalog.ListModels(nil); err != nil {
				t.Fatal("catalog driver became unusable", err)
			}
		})
	}
}
