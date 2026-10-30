package service_test

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/cli"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/service"
	"multirag/internal/testsupport"
)

func deepSeekDiscoveryServer(t *testing.T, j *testsupport.CaseJournal, label string, h http.Handler) *httptest.Server {
	t.Helper()
	var listener net.Listener
	for range 30 {
		address := fmt.Sprintf("127.0.0.1:%d", 21000+time.Now().UnixNano()%29000)
		j.Event("reserve-listener", map[string]any{"label": label, "address": address})
		var err error
		listener, err = net.Listen("tcp", address)
		if err == nil {
			break
		}
		j.Event("listener-bind-failed", map[string]any{"label": label, "address": address})
	}
	if listener == nil {
		t.Fatal("no registered listener available")
	}
	srv := &httptest.Server{Listener: listener, Config: &http.Server{Handler: h}}
	srv.Start()
	t.Cleanup(func() {
		srv.CloseClientConnections()
		srv.Close()
		address := listener.Addr().String()
		conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
		if conn != nil {
			conn.Close()
		}
		j.Event("cleanup-listener", map[string]any{"label": label, "address": address, "absent": err != nil})
		if err == nil {
			t.Error("listener survived cleanup")
		}
	})
	return srv
}

func deepSeekDiscoveryDatabase(t *testing.T, j *testsupport.CaseJournal, path string) (*gorm.DB, *sql.DB) {
	t.Helper()
	var cfg struct {
		AdminDSN   string `json:"admin_dsn"`
		PGEndpoint string `json:"pg_endpoint"`
	}
	raw, err := os.ReadFile(path)
	if err != nil || json.Unmarshal(raw, &cfg) != nil {
		t.Fatal("private discovery database config unavailable")
	}
	parsed, err := url.Parse(cfg.AdminDSN)
	if err != nil || parsed.Host != cfg.PGEndpoint || parsed.Hostname() != "127.0.0.1" || strings.Trim(parsed.Path, "/") != "postgres" {
		t.Fatal("explicit local PostgreSQL admin database required")
	}
	admin, err := sql.Open("pgx", cfg.AdminDSN)
	if err != nil {
		t.Fatal("admin connection unavailable")
	}
	t.Cleanup(func() { admin.Close() })
	name := "multirag_after10bbe_m_" + strings.ReplaceAll(uuid.NewString(), "-", "")[:16]
	j.Event("reserve-database", map[string]any{"database": name, "endpoint": cfg.PGEndpoint, "config": path})
	var present bool
	if err = admin.QueryRowContext(t.Context(), "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&present); err != nil || present {
		t.Fatal("scratch database precondition failed")
	}
	if _, err = admin.ExecContext(t.Context(), `CREATE DATABASE "`+name+`"`); err != nil {
		t.Fatal("scratch database creation failed")
	}
	t.Cleanup(func() {
		_, dropErr := admin.Exec(`DROP DATABASE "` + name + `" WITH (FORCE)`)
		readErr := admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&present)
		j.Event("cleanup-database", map[string]any{"database": name, "absent": !present, "dropOK": dropErr == nil, "readOK": readErr == nil})
		if dropErr != nil || readErr != nil || present {
			t.Error("scratch database cleanup failed")
		}
	})
	parsed.Path = "/" + name
	db, err := gorm.Open(postgres.Open(parsed.String()), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("scratch database connection failed")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal("scratch pool unavailable")
	}
	t.Cleanup(func() { pool.Close() })
	read, err := sql.Open("pgx", parsed.String())
	if err != nil {
		t.Fatal("independent readback connection unavailable")
	}
	t.Cleanup(func() { read.Close() })
	return db, read
}

// This exercises the real catalog, SQL ownership checks, Gitee driver, handler,
// and CLI parser/client. Only remote inventory and route identity are controlled.
func TestDeepSeekCatalogDiscoveryConsumersHTTPAndSQL(t *testing.T) {
	config := os.Getenv("MULTIRAG_DEEPSEEK_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned DeepSeek discovery PostgreSQL config required")
		}
		t.Skip("requires owned DeepSeek discovery PostgreSQL config")
	}
	j := testsupport.NewCase(t)
	db, read := deepSeekDiscoveryDatabase(t, j, config)
	previous := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = previous })
	if err := db.AutoMigrate(&entity.UserTenant{}, &entity.TenantModelProvider{}, &entity.TenantModelInstance{}, &entity.TenantModel{}); err != nil {
		t.Fatal("scratch schema creation failed")
	}
	pm, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	if err := pm.LoadAllModels(filepath.Join("..", "..", "configs", "all_models.json")); err != nil {
		t.Fatal(err)
	}
	var ids []string
	var expected []entity.SupportedModel
	seen := map[string]bool{}
	models := 0
	for _, model := range pm.ListAllModels() {
		if !strings.HasPrefix(model.Name, "deepseek-") {
			continue
		}
		models++
		for _, alias := range append([]string{model.Name}, model.Alias...) {
			for _, rawID := range []string{alias, strings.ToLower(alias), strings.ToUpper(alias)} {
				if seen[rawID] {
					continue
				}
				seen[rawID] = true
				ids = append(ids, rawID)
				tokens, types := model.MaxTokens, append([]string{}, model.ModelTypes...)
				expected = append(expected, entity.SupportedModel{Name: rawID, MaxTokens: &tokens, ModelTypes: &types, Thinking: model.Thinking})
			}
		}
	}
	if models != 70 {
		t.Fatalf("DeepSeek catalog entries = %d, want 70", models)
	}
	ids = append(ids, "unknown-provider-model")
	expected = append(expected, entity.SupportedModel{Name: "unknown-provider-model"})
	var fail atomic.Bool
	var calls atomic.Int32
	upstream := deepSeekDiscoveryServer(t, j, "gitee", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Method != http.MethodGet || r.URL.Path != "/models" || r.ContentLength > 0 || r.Header.Get("Authorization") != "" {
			t.Error("public discovery changed path, method, body or sent credentials")
			w.WriteHeader(400)
			return
		}
		if fail.Load() {
			fmt.Fprint(w, `{"data":[{"id":"deepseek-r1"},{"id":17}]}`)
			return
		}
		rows := make([]map[string]string, 0, len(ids))
		for _, id := range ids {
			rows = append(rows, map[string]string{"id": id, "owned_by": "gitee"})
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
			var hash string
			if err := read.QueryRowContext(t.Context(), "SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY id)::text,'')) FROM "+table+" t").Scan(&hash); err != nil {
				t.Fatal("independent SQL readback failed")
			}
			result += hash
		}
		return result
	}
	before := snapshot()
	svc := service.NewModelProviderServiceWithCatalog(pm, nil)
	rows, err := svc.ListSupportedModelsWithContext(t.Context(), "Gitee", "primary", "owner")
	if err != nil || !reflect.DeepEqual(rows, expected) {
		t.Fatalf("service metadata/order/raw IDs differ: rows=%d expected=%d error=%v", len(rows), len(expected), err)
	}
	*rows[0].MaxTokens = 1
	rows[0].Thinking.DefaultValue = false
	(*rows[0].ModelTypes)[0] = "changed"
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.Use(func(c *gin.Context) {
		if strings.TrimPrefix(c.GetHeader("Authorization"), "Bearer ") != "fixture-catalog-owner" {
			c.AbortWithStatus(http.StatusUnauthorized)
			return
		}
		c.Set("user_id", "owner")
		c.Set("user", &entity.User{ID: "owner"})
		c.Next()
	})
	app.GET("/api/v1/providers/:provider_name/instances/:instance_name/models", handler.NewProviderHandler(nil, svc).ListInstanceModels)
	app.GET("/api/v1/all-models/detail", handler.NewModelHandler(svc).ShowModel)
	api := deepSeekDiscoveryServer(t, j, "application", app)
	u, _ := url.Parse(api.URL)
	port, _ := strconv.Atoi(u.Port())
	client := cli.NewMultiRAGClient("user")
	client.HTTPClient.Host, client.HTTPClient.Port, client.HTTPClient.LoginToken = u.Hostname(), port, "fixture-catalog-owner"
	command, err := cli.NewParser(`LIST SUPPORTED MODELS FROM 'Gitee' 'primary';`).Parse(false)
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
		t.Fatal("handler/CLI lost metadata or changed inventory identity")
	}
	show, err := cli.NewParser(`SHOW MODEL 'DEEPSEEK-AI/DEEPSEEK-OCR-2';`).Parse(false)
	if err != nil {
		t.Fatal(err)
	}
	shown, err := client.ExecuteCommand(show)
	if err != nil {
		t.Fatal(err)
	}
	detail, ok := shown.(*cli.CommonDataResponse)
	if !ok || detail.Data["name"] != "deepseek-ocr-2" || detail.Data["max_tokens"] != float64(8192) {
		t.Fatal("SHOW MODEL failed mixed-case catalog alias")
	}
	remoteBefore := calls.Load()
	if result, err := svc.ListSupportedModelsWithContext(t.Context(), "Gitee", "primary", "foreign"); err == nil || result != nil || calls.Load() != remoteBefore {
		t.Fatal("foreign ownership reached provider")
	}
	fail.Store(true)
	if result, err := client.ExecuteCommand(command); err == nil || result != nil {
		t.Fatal("malformed provider inventory reported partial success")
	}
	if snapshot() != before {
		t.Fatal("success or failure changed SQL inventory/bindings")
	}
	j.Event("deepseek-consumer-contract", map[string]any{"models": models, "inventoryRows": len(expected), "knownAliasesAndCapabilities": true, "caseInsensitive": true, "unknownOmitted": true, "rawIDsAndOrderPreserved": true, "ownedByIgnored": true, "SQLUnchanged": true, "snapshotBefore": before, "snapshotAfter": snapshot(), "cliParserClient": true, "identityBoundary": "controlled route middleware"})
}
