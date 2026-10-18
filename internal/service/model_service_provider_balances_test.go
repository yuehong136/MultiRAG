package service

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"testing"

	"multirag/internal/entity"
)

func testProviderBalanceInstances(t *testing.T, providerName, suffix, body string, want map[string]interface{}) {
	t.Helper()
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	provider := manager.FindProvider(providerName)
	if provider == nil || provider.URLSuffix.Balance != suffix {
		t.Fatal("catalog balance endpoint is missing")
	}
	original := provider.URL["default"]
	for _, instance := range []string{"one", "two"} {
		t.Run(instance, func(t *testing.T) {
			t.Parallel()
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet || r.URL.Path != "/"+instance+"/"+suffix || r.Header.Get("Authorization") != "Bearer "+instance {
					t.Error("bound balance endpoint/key lost")
				}
				fmt.Fprint(w, body)
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
			got, err := driver.Balance(api)
			if err != nil || !reflect.DeepEqual(got, want) {
				t.Fatal(got, err)
			}
			if provider.URL["default"] != original {
				t.Fatal("shared catalog URL mutated")
			}
			t.Logf("%s instance %s: actual GET %s returned validated balance through configured driver", providerName, instance, suffix)
		})
	}
}

func TestSiliconFlowBalanceConfiguredInstancesHTTP(t *testing.T) {
	testProviderBalanceInstances(t, "SiliconFlow", "user/info", `{"code":0,"status":true,"data":{"totalBalance":"5.75","balance":"1"}}`, map[string]interface{}{"balance": 5.75, "currency": "CNY"})
}
