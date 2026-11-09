package service_test

import (
	modelModule "multirag/internal/entity/models"

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

// The real inventory driver, SQL ownership checks, metadata service, handlers and
// CLI consume every catalog alias. Provider HTTP and route identity are controlled.
func TestExpandedCatalogDiscoveryConsumersHTTPAndSQL(t *testing.T) {
	config := os.Getenv("MULTIRAG_QWEN_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned Qwen discovery PostgreSQL config required")
		}
		t.Skip("requires owned Qwen discovery PostgreSQL config")
	}
	j := testsupport.NewCase(t)
	db, read := deepSeekDiscoveryDatabase(t, j, config)
	previous := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = previous })
	if err := db.AutoMigrate(&entity.UserTenant{}, &entity.TenantModelProvider{}, &entity.TenantModelInstance{}, &entity.TenantModel{}); err != nil {
		t.Fatal("scratch schema creation failed")
	}
	pm, err := modelModule.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	if err := pm.LoadAllModels(filepath.Join("..", "..", "configs", "all_models.json")); err != nil {
		t.Fatal(err)
	}
	var ids []string
	var expected []modelModule.SupportedModel
	seen := map[string]bool{}
	count := 0
	for _, model := range pm.ListAllModels() {
		count++
		for _, alias := range append([]string{model.Name}, model.Alias...) {
			for _, rawID := range []string{alias, strings.ToUpper(alias)} {
				if seen[rawID] {
					continue
				}
				seen[rawID] = true
				ids = append(ids, rawID)
				types := append([]string{}, model.ModelTypes...)
				row := modelModule.SupportedModel{Name: rawID, MaxDimension: model.MaxDimension, ModelTypes: &types, Thinking: model.Thinking}
				if model.Dimensions != nil {
					row.Dimensions = &model.Dimensions
				}
				if model.MaxTokens != 0 {
					tokens := model.MaxTokens
					row.MaxTokens = &tokens
				}
				expected = append(expected, row)
			}
		}
	}
	if count != 1124 {
		t.Fatalf("catalog entries = %d, want 1124", count)
	}
	ids = append(ids, "unknown-provider-qwen")
	expected = append(expected, modelModule.SupportedModel{Name: "unknown-provider-qwen"})
	var fail atomic.Bool
	var calls atomic.Int32
	upstream := deepSeekDiscoveryServer(t, j, "qwen-inventory", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Method != http.MethodGet || r.URL.Path != "/models" || r.ContentLength > 0 || r.Header.Get("Authorization") != "" {
			t.Error("public discovery changed wire contract or sent credentials")
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if fail.Load() {
			fmt.Fprint(w, `{"data":[{"id":"Qwen/Qwen3-32B"},{"id":17}]}`)
			return
		}
		rows := make([]map[string]string, 0, len(ids))
		for _, id := range ids {
			rows = append(rows, map[string]string{"id": id, "owned_by": "controlled-provider"})
		}
		if err := json.NewEncoder(w).Encode(map[string]any{"data": rows}); err != nil {
			t.Error(err)
		}
	}))
	active := "1"
	if err := db.Create(&entity.UserTenant{ID: "owner", UserID: "owner", TenantID: "tenant", Role: "owner", InvitedBy: "owner", Status: &active}).Error; err != nil {
		t.Fatal("fixture ownership creation failed")
	}
	if err := db.Create(&entity.TenantModelProvider{ID: "gitee", ProviderName: "Gitee", TenantID: "tenant"}).Error; err != nil {
		t.Fatal("fixture provider creation failed")
	}
	extra, _ := json.Marshal(map[string]string{"base_url": upstream.URL, "region": "default"})
	if err := db.Create(&entity.TenantModelInstance{ID: "primary", ProviderID: "gitee", InstanceName: "primary", APIKey: "fixture-private-key", Extra: string(extra), Status: "active"}).Error; err != nil {
		t.Fatal("fixture instance creation failed")
	}
	snapshot := func() string {
		t.Helper()
		var result string
		for _, table := range []string{"t_ai_user_tenants", "tenant_model_provider", "tenant_model_instance", "tenant_model"} {
			var digest string
			if err := read.QueryRowContext(t.Context(), "SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY id)::text,'')) FROM "+table+" t").Scan(&digest); err != nil {
				t.Fatal("independent SQL readback failed")
			}
			result += digest
		}
		return result
	}
	before := snapshot()
	svc := service.NewModelProviderServiceWithCatalog(pm, nil)
	rows, err := svc.ListSupportedModelsWithContext(t.Context(), "Gitee", "primary", "owner")
	if err != nil || !reflect.DeepEqual(rows, expected) {
		t.Fatalf("service metadata/order/raw IDs differ: rows=%d expected=%d error=%v", len(rows), len(expected), err)
	}
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.Use(func(c *gin.Context) {
		if strings.TrimPrefix(c.GetHeader("Authorization"), "Bearer ") != "fixture-qwen-owner" {
			c.AbortWithStatus(http.StatusUnauthorized)
			return
		}
		c.Set("user_id", "owner")
		c.Set("user", &entity.User{ID: "owner"})
		c.Next()
	})
	h := handler.NewModelHandler(svc)
	for _, prefix := range []string{"/api/v1", "/api/v1/admin"} {
		app.GET(prefix+"/all-models", h.ListAllModels)
		app.GET(prefix+"/all-models/detail", h.ShowModel)
	}
	app.GET("/api/v1/providers/:provider_name/instances/:instance_name/models", handler.NewProviderHandler(nil, svc).ListInstanceModels)
	api := deepSeekDiscoveryServer(t, j, "qwen-application", app)
	u, _ := url.Parse(api.URL)
	port, _ := strconv.Atoi(u.Port())
	for _, mode := range []string{"user", "admin"} {
		client := cli.NewMultiRAGClient(mode)
		client.HTTPClient.Host, client.HTTPClient.Port, client.HTTPClient.LoginToken = u.Hostname(), port, "fixture-qwen-owner"
		command, err := cli.NewParser(`LIST MODELS;`).Parse(mode == "admin")
		if err != nil {
			t.Fatal(err)
		}
		result, err := client.ExecuteCommand(command)
		if err != nil {
			t.Fatal(err)
		}
		list, ok := result.(*cli.CommonResponse)
		if !ok || len(list.Data) != 1124 {
			t.Fatalf("%s CLI catalog cardinality: %T", mode, result)
		}
		for _, tc := range []struct {
			alias, name string
			tokens      float64
		}{
			{"QWEN/QWEN3-ASR-1.7B", "qwen/qwen3-asr-1.7b", 65536},
			{"qwen3-asr-1.7b", "qwen/qwen3-asr-1.7b", 65536},
			{"Qwen3-Embedding-8B", "qwen/qwen3-embedding-8b", 32768},
			{"QWEN/QWEN3.6-27B", "qwen/qwen3.6-27b", 262144},
			{"DEEPSEEK-CHAT", "deepseek-v4-flash", 1048576},
			{"qwen-plus", "qwen/qwen-plus", 1000000},
			{"z-ai/glm-4.5v", "zai-org/glm-4.5v", 128000},
			{"embedding-3", "zai-org/embedding-3", 8192},
			{"jina-embeddings-v3", "jinaai/jina-embeddings-v3", 8192},
		} {
			show, err := cli.NewParser("SHOW MODEL '" + tc.alias + "';").Parse(mode == "admin")
			if err != nil {
				t.Fatal(err)
			}
			shown, err := client.ExecuteCommand(show)
			if err != nil {
				t.Fatal(err)
			}
			detail, ok := shown.(*cli.CommonDataResponse)
			if !ok || detail.Data["name"] != tc.name || detail.Data["max_tokens"] != tc.tokens {
				t.Fatalf("%s SHOW MODEL %q lost metadata", mode, tc.alias)
			}
		}
		if mode == "user" {
			command, err = cli.NewParser(`LIST SUPPORTED MODELS FROM 'Gitee' 'primary';`).Parse(false)
			if err != nil {
				t.Fatal(err)
			}
			result, err = client.ExecuteCommand(command)
			if err != nil {
				t.Fatal(err)
			}
			response, ok := result.(*cli.CommonResponse)
			if !ok {
				t.Fatalf("unexpected CLI response %T", result)
			}
			encoded, err := json.Marshal(response.Data)
			var discovered []modelModule.SupportedModel
			if err != nil || json.Unmarshal(encoded, &discovered) != nil || !reflect.DeepEqual(discovered, expected) {
				t.Fatal("handler/CLI lost metadata or changed inventory identity")
			}
			fail.Store(true)
			if result, err := client.ExecuteCommand(command); err == nil || result != nil {
				t.Fatal("malformed provider inventory reported partial success")
			}
		}
	}
	remoteBefore := calls.Load()
	if result, err := svc.ListSupportedModelsWithContext(t.Context(), "Gitee", "primary", "foreign"); err == nil || result != nil || calls.Load() != remoteBefore {
		t.Fatal("foreign ownership reached provider")
	}
	for _, target := range []string{"/api/v1/all-models", "/api/v1/admin/all-models"} {
		response, err := http.Get(api.URL + target)
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Close()
		if response.StatusCode != http.StatusUnauthorized {
			t.Fatal("unauthenticated catalog accepted")
		}
	}
	if snapshot() != before {
		t.Fatal("catalog, discovery, success or failure changed SQL inventory/bindings")
	}
	j.Event("qwen-consumer-contract", map[string]any{"models": count, "inventoryRows": len(expected), "knownAliasesAndCapabilities": true, "rawIDsAndOrderPreserved": true, "SQLUnchanged": true, "snapshotBefore": before, "snapshotAfter": snapshot(), "cliParserClient": true, "userAdminListShow": true, "identityBoundary": "controlled route middleware", "providerBoundary": "controlled HTTP inventory only"})
}
