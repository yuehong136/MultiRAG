package service_test

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/cli"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/service"
	"multirag/internal/testsupport"
)

// Real SQL, factory, discovery, REST handler and CLI parser/client; only the
// provider HTTP response and authenticated route identity are controlled.
func TestSiliconFlowDiscoveryConsumersHTTPAndSQL(t *testing.T) {
	config := os.Getenv("MULTIRAG_DISCOVERY_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned discovery PostgreSQL config required")
		}
		t.Skip("requires owned discovery PostgreSQL config")
	}
	j := testsupport.NewCase(t)
	db, read := deepSeekDiscoveryDatabase(t, j, config)
	previous := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = previous })
	if err := db.AutoMigrate(&entity.UserTenant{}, &entity.TenantModelProvider{}, &entity.TenantModelInstance{}, &entity.TenantModel{}); err != nil {
		t.Fatal(err)
	}
	catalogRoot := os.Getenv("MULTIRAG_DISCOVERY_CATALOG_ROOT")
	if catalogRoot == "" {
		catalogRoot = filepath.Join("..", "..", "configs")
	}
	pm, err := entity.NewProviderManager(filepath.Join(catalogRoot, "models"))
	if err != nil {
		t.Fatal(err)
	}
	if err := pm.LoadAllModels(filepath.Join(catalogRoot, "all_models.json")); err != nil {
		t.Fatal(err)
	}
	provider := pm.FindProvider("SiliconFlow")
	if provider == nil || len(provider.Models) == 0 {
		t.Fatal("SiliconFlow catalog unavailable")
	}
	knownID := provider.Models[0].Name
	expected := pm.DescribeSupportedModels("SiliconFlow", []string{knownID})
	if expected[0].MaxTokens == nil || expected[0].ModelTypes == nil {
		t.Fatal("known model metadata unavailable")
	}
	expected[0].Name += "@owner"
	var zero entity.Model
	if err := json.Unmarshal([]byte(`{"name":"controlled-zero","max_tokens":0,"max_dimension":0,"model_types":[],"thinking":{"default_value":false,"clear_thinking":false}}`), &zero); err != nil {
		t.Fatal(err)
	}
	provider.Models = append(provider.Models, &zero)
	zeroRow := pm.DescribeSupportedModels("SiliconFlow", []string{zero.Name})[0]
	zeroRow.Name += "@owner"
	expected = append(expected, entity.SupportedModel{Name: "unknown@owner"}, zeroRow)
	var mode atomic.Int32
	var calls atomic.Int32
	upstream := deepSeekDiscoveryServer(t, j, "siliconflow-provider", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Method != http.MethodGet || r.URL.Path != "/instance/models" || r.ContentLength > 0 || r.Header.Get("Authorization") != "Bearer fixture-private-key" {
			t.Error("discovery request changed instance, credentials or bodyless GET")
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		switch mode.Load() {
		case 1:
			fmt.Fprint(w, `{"data":[{"id":"first"},{"id":17}]}`)
		case 2:
			fmt.Fprint(w, `{"data":[]}`)
		default:
			if err := json.NewEncoder(w).Encode(map[string]any{"data": []map[string]string{{"id": knownID, "owned_by": "owner"}, {"id": "unknown", "owned_by": "owner"}, {"id": zero.Name, "owned_by": "owner"}}}); err != nil {
				t.Error(err)
			}
		}
	}))
	active := "1"
	if err := db.Create(&entity.UserTenant{ID: "owner", UserID: "owner", TenantID: "tenant", Role: "owner", InvitedBy: "owner", Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Create(&entity.TenantModelProvider{ID: "siliconflow", ProviderName: "SiliconFlow", TenantID: "tenant"}).Error; err != nil {
		t.Fatal(err)
	}
	extra, _ := json.Marshal(map[string]string{"base_url": upstream.URL + "/instance", "region": "default"})
	if err := db.Create(&entity.TenantModelInstance{ID: "primary", ProviderID: "siliconflow", InstanceName: "primary", APIKey: "fixture-private-key", Extra: string(extra), Status: "active"}).Error; err != nil {
		t.Fatal(err)
	}
	snapshot := func() string {
		t.Helper()
		var snapshot string
		for _, table := range []string{"t_ai_user_tenants", "tenant_model_provider", "tenant_model_instance", "tenant_model"} {
			var hash string
			if err := read.QueryRowContext(t.Context(), "SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY id)::text,'')) FROM "+table+" t").Scan(&hash); err != nil {
				t.Fatal(err)
			}
			snapshot += hash
		}
		return snapshot
	}
	before := snapshot()
	svc := service.NewModelProviderServiceWithCatalog(pm, nil)
	rows, err := svc.ListSupportedModelsWithContext(t.Context(), "SiliconFlow", "primary", "owner")
	if err != nil || !reflect.DeepEqual(rows, expected) {
		t.Fatal("driver to service lost owned-ID metadata", rows, expected, err)
	}
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.Use(func(c *gin.Context) {
		if strings.TrimPrefix(c.GetHeader("Authorization"), "Bearer ") != "fixture-owner" {
			c.AbortWithStatus(http.StatusUnauthorized)
			return
		}
		c.Set("user_id", "owner")
		c.Set("user", &entity.User{ID: "owner"})
		c.Next()
	})
	app.GET("/api/v1/providers/:provider_name/instances/:instance_name/models", handler.NewProviderHandler(nil, svc).ListInstanceModels)
	api := deepSeekDiscoveryServer(t, j, "siliconflow-application", app)
	u, _ := url.Parse(api.URL)
	port, _ := strconv.Atoi(u.Port())
	client := cli.NewMultiRAGClient("user")
	client.HTTPClient.Host, client.HTTPClient.Port, client.HTTPClient.LoginToken = u.Hostname(), port, "fixture-owner"
	command, err := cli.NewParser(`LIST SUPPORTED MODELS FROM 'SiliconFlow' 'primary';`).Parse(false)
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.ExecuteCommand(command)
	if err != nil {
		t.Fatal(err)
	}
	response, ok := result.(*cli.CommonResponse)
	if !ok {
		t.Fatalf("unexpected CLI response %T", result)
	}
	encoded, err := json.Marshal(response.Data)
	var discovered []entity.SupportedModel
	if err != nil || json.Unmarshal(encoded, &discovered) != nil || !reflect.DeepEqual(discovered, expected) {
		t.Fatal("REST/CLI changed metadata or discovery IDs", string(encoded))
	}
	remoteBefore := calls.Load()
	if result, err := svc.ListSupportedModelsWithContext(t.Context(), "SiliconFlow", "primary", "foreign"); err == nil || result != nil || calls.Load() != remoteBefore {
		t.Fatal("foreign user reached provider")
	}
	mode.Store(1)
	if result, err := client.ExecuteCommand(command); err == nil || result != nil {
		t.Fatal("invalid provider response returned partial CLI success")
	}
	mode.Store(2)
	empty, err := svc.ListSupportedModelsWithContext(t.Context(), "SiliconFlow", "primary", "owner")
	if err != nil || empty == nil || len(empty) != 0 {
		t.Fatal("empty inventory is not []", empty, err)
	}
	if snapshot() != before {
		t.Fatal("discovery success/failure changed SQL state")
	}
	j.Event("siliconflow-discovery-contract", map[string]any{"rows": len(expected), "namesUnchanged": true, "metadataPresence": true, "CLIHTTPConsumer": true, "SQLSnapshotBefore": before, "SQLSnapshotAfter": snapshot(), "identityBoundary": "controlled route middleware"})
}
