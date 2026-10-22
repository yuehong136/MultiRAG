package service

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"

	"multirag/internal/entity"
)

func TestVolcEngineAliyunDiscoveryInstanceIsolationHTTP(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"VolcEngine", "Aliyun"} {
		t.Run(name, func(t *testing.T) {
			catalog := manager.FindProvider(name)
			if catalog == nil || catalog.URLSuffix.Models == "" {
				t.Fatal("catalog discovery endpoint missing")
			}
			if name == "VolcEngine" && catalog.URLSuffix.Models != "models" {
				t.Fatalf("Ark models suffix = %q", catalog.URLSuffix.Models)
			}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				parts := strings.SplitN(strings.TrimPrefix(r.URL.Path, "/"), "/", 2)
				if len(parts) != 2 {
					t.Error("missing instance path", r.URL.Path)
					w.WriteHeader(http.StatusNotFound)
					return
				}
				id, suffix := parts[0], parts[1]
				if r.Header.Get("Authorization") != "Bearer "+id+"-key" {
					t.Error("credentials crossed instances")
				}
				switch suffix {
				case catalog.URLSuffix.Models:
					if r.Method != http.MethodGet {
						t.Error("discovery method", r.Method)
					}
					fmt.Fprintf(w, `{"data":[{"id":%q,"owned_by":"vendor"}]}`, id)
				case catalog.URLSuffix.Files:
					fmt.Fprint(w, `{"data":[]}`)
				default:
					t.Error("unexpected provider path", r.URL.Path)
					w.WriteHeader(http.StatusNotFound)
				}
			}))
			defer server.Close()
			urls := map[string]string{"default": server.URL + "/default/", "singapore": server.URL + "/singapore/"}
			provider := &entity.Provider{Name: name, URL: urls, URLSuffix: catalog.URLSuffix, ModelDriver: catalog.ModelDriver}
			var group sync.WaitGroup
			for _, test := range []struct{ region, base, id string }{
				{"", "", "default"},
				{"unknown", "", "default"},
				{"singapore", "", "singapore"},
				{"one", server.URL + "/one/", "one"},
				{"two", server.URL + "/two/", "two"},
			} {
				group.Go(func() {
					raw, err := encodeProviderInstanceExtra(test.region, test.base)
					if err != nil {
						t.Error(err)
						return
					}
					key := test.id + "-key"
					if name == "VolcEngine" {
						key = fmt.Sprintf(`{"ark_api_key":%q,"endpoint_id":"ep"}`, key)
					}
					driver, config, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", APIKey: key, Extra: raw})
					if err != nil {
						t.Error(err)
						return
					}
					for range 3 {
						if err := driver.CheckConnection(config); err != nil {
							t.Error(err)
							return
						}
						if got, err := driver.ListModels(config); err != nil || !reflect.DeepEqual(got, []string{test.id}) {
							t.Error(got, err)
							return
						}
					}
				})
			}
			group.Wait()
			if provider.ModelDriver != catalog.ModelDriver || !reflect.DeepEqual(urls, map[string]string{"default": server.URL + "/default/", "singapore": server.URL + "/singapore/"}) {
				t.Fatal("shared provider state changed")
			}
			if _, _, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", Extra: `{}`}); err == nil {
				t.Fatal("cloud provider instance accepted missing credentials")
			}
		})
	}
}
