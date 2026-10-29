package handler_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/testsupport"
	"multirag/internal/utility"
)

type discoveryCountingFactory struct{ calls atomic.Int32 }

func (f *discoveryCountingFactory) CreateModelDriver(name string, urls map[string]string, suffix models.URLSuffix) (models.ModelDriver, error) {
	f.calls.Add(1)
	return models.NewModelFactory().CreateModelDriver(name, urls, suffix)
}

func discoveryMetadataServer(t *testing.T, j *testsupport.CaseJournal, label string, h http.Handler) *httptest.Server {
	t.Helper()
	var listener net.Listener
	for range 20 {
		address := fmt.Sprintf("127.0.0.1:%d", 21000+time.Now().UnixNano()%29000)
		j.Event("reserve-listener", map[string]any{"label": label, "address": address})
		if conn, err := net.DialTimeout("tcp", address, 50*time.Millisecond); err == nil {
			conn.Close()
			continue
		}
		var err error
		listener, err = net.Listen("tcp", address)
		if err == nil {
			break
		}
	}
	if listener == nil {
		t.Fatal("no free registered listener")
	}
	address := listener.Addr().String()
	srv := &httptest.Server{Listener: listener, Config: &http.Server{Handler: h}}
	srv.Start()
	t.Cleanup(func() {
		srv.CloseClientConnections()
		srv.Close()
		conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
		if conn != nil {
			conn.Close()
		}
		j.Event("cleanup-listener", map[string]any{"label": label, "address": address, "absent": err != nil})
		if err == nil {
			t.Error("listener remains", address)
		}
	})
	return srv
}

// Real JWT router, SQL ownership/instance binding, provider drivers and optional
// CLI process; external provider inventory is controlled and never paid.
func TestSupportedDiscoveryMetadataHTTPAndSQL(t *testing.T) {
	path := os.Getenv("MULTIRAG_DISCOVERY_CONFIG")
	if path == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned discovery PostgreSQL config required")
		}
		t.Skip("requires owned discovery PostgreSQL config")
	}
	j := testsupport.NewCase(t)
	var cfg struct {
		AdminDSN   string `json:"admin_dsn"`
		PGEndpoint string `json:"pg_endpoint"`
	}
	raw, err := os.ReadFile(path)
	if err != nil || json.Unmarshal(raw, &cfg) != nil {
		t.Fatal("private discovery config unavailable")
	}
	parsed, err := url.Parse(cfg.AdminDSN)
	if err != nil || parsed.Host != "127.0.0.1:5432" || cfg.PGEndpoint != parsed.Host {
		t.Fatal("explicit local TCP database required")
	}
	admin, err := sql.Open("pgx", cfg.AdminDSN)
	if err != nil {
		t.Fatal("admin open")
	}
	t.Cleanup(func() { admin.Close() })
	name := "multirag_after298_m_" + strings.ReplaceAll(uuid.NewString(), "-", "")[:16]
	j.Event("reserve-database", map[string]any{"database": name, "endpoint": cfg.PGEndpoint, "config": path})
	var dbIdentity string
	if err = admin.QueryRow("SELECT version() || ' at ' || inet_server_addr()::text || ':' || inet_server_port()::text").Scan(&dbIdentity); err != nil {
		t.Fatal("database identity readback")
	}
	j.Event("borrowed-postgres-identity", map[string]any{"identity": dbIdentity})
	var present bool
	if err = admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&present); err != nil || present {
		t.Fatal("database precondition")
	}
	if _, err = admin.Exec(`CREATE DATABASE "` + name + `"`); err != nil {
		t.Fatal("create owned database", err)
	}
	t.Cleanup(func() {
		_, dropErr := admin.Exec(`DROP DATABASE "` + name + `" WITH (FORCE)`)
		readErr := admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&present)
		j.Event("cleanup-database", map[string]any{"database": name, "absent": !present, "drop_ok": dropErr == nil, "read_ok": readErr == nil})
		if dropErr != nil || readErr != nil || present {
			t.Error("owned database cleanup failed")
		}
	})
	parsed.Path = "/" + name
	db, err := gorm.Open(postgres.Open(parsed.String()), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("owned database connect")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	read, err := sql.Open("pgx", parsed.String())
	if err != nil {
		t.Fatal("readback open")
	}
	t.Cleanup(func() { read.Close() })
	previous := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = previous })
	if err = db.AutoMigrate(&entity.User{}, &entity.UserTenant{}, &entity.TenantModelProvider{}, &entity.TenantModelInstance{}, &entity.TenantModel{}); err != nil {
		t.Fatal(err)
	}
	if err = common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err = server.InitVariables(statusSigningStore{secret: strings.Repeat("owned-discovery-", 4)}); err != nil {
		t.Fatal(err)
	}
	local.InitAdminStatus(0, "owned metadata discovery acceptance")
	active := "1"
	tokens := map[string]string{}
	for _, id := range []string{"owner", "foreign", "member"} {
		email := id + "@discovery.invalid"
		if err = db.Create(&entity.User{ID: id, Email: email, Nickname: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		role := "owner"
		if id == "member" {
			role = "normal"
		}
		if err = db.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: id + "-tenant", Role: role, InvitedBy: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		token, err := utility.SignUserJWT(email, server.GetSecretKey())
		if err != nil {
			t.Fatal(err)
		}
		tokens[id] = token
	}
	calls := atomic.Int32{}
	started := make(chan struct{}, 2)
	cancelled := make(chan struct{}, 2)
	upstream := discoveryMetadataServer(t, j, "provider", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
		expectedKey := ""
		if len(parts) == 3 && parts[0] == "openai" {
			expectedKey = "Bearer " + parts[1] + "-key"
		}
		if len(parts) != 3 || parts[2] != "models" || r.Method != http.MethodGet || r.Header.Get("Authorization") != expectedKey {
			t.Error("wrong instance URL/method/key")
			w.WriteHeader(400)
			return
		}
		switch parts[1] {
		case "one", "two":
			fmt.Fprint(w, `{"data":[{"id":"namespace/raw","owned_by":"vendor"},{"id":"unknown"},{"id":"zero"}]}`)
		case "bad":
			fmt.Fprint(w, `{"data":[{"id":"valid"},{"id":17}]}`)
		case "business":
			fmt.Fprint(w, `{"error":{"message":"private-provider-error"},"data":[{"id":"valid"}]}`)
		case "empty":
			fmt.Fprint(w, `{"data":[]}`)
		case "null":
			fmt.Fprint(w, `{"data":null}`)
		case "slow":
			started <- struct{}{}
			<-r.Context().Done()
			cancelled <- struct{}{}
		default:
			t.Error("unexpected inventory path")
			w.WriteHeader(404)
		}
	}))
	pm, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	catalogPath := filepath.Join(t.TempDir(), "catalog.json")
	j.Event("reserve-catalog", map[string]any{"path": catalogPath})
	if err = os.WriteFile(catalogPath, []byte(`[{"name":"canonical","alias":["namespace/raw"],"max_tokens":8192,"dimension":128,"model_types":["embedding","chat"],"thinking":{"default_value":false,"clear_thinking":false}},{"name":"zero","max_tokens":0,"dimension":0,"model_types":[]}]`), 0600); err != nil {
		t.Fatal(err)
	}
	if err = pm.LoadAllModels(catalogPath); err != nil {
		t.Fatal(err)
	}
	for _, providerName := range []string{"Gitee", "OpenAI"} {
		provider := pm.FindProvider(providerName)
		provider.URL = map[string]string{"default": upstream.URL + "/" + strings.ToLower(providerName) + "/one", "other": upstream.URL + "/" + strings.ToLower(providerName) + "/two"}
		provider.URLSuffix.Models = "models"
		providerID := strings.ToLower(providerName)
		if err = db.Create(&entity.TenantModelProvider{ID: providerID, ProviderName: providerName, TenantID: "owner-tenant"}).Error; err != nil {
			t.Fatal(err)
		}
		for _, instance := range []string{"one", "two", "bad", "business", "empty", "null", "slow", "region", "disabled", "keyless"} {
			base := upstream.URL + "/" + strings.ToLower(providerName) + "/" + instance
			region := "default"
			key := instance + "-key"
			status := "active"
			if instance == "two" {
				base = ""
				region = "other"
			}
			if instance == "region" {
				base = ""
				region = "unknown"
			}
			if instance == "disabled" {
				status = "disabled"
			}
			if instance == "keyless" {
				key = ""
			}
			extra, _ := json.Marshal(map[string]string{"base_url": base, "region": region})
			if err = db.Create(&entity.TenantModelInstance{ID: providerID + "-" + instance, InstanceName: instance, ProviderID: providerID, APIKey: key, Extra: string(extra), Status: status}).Error; err != nil {
				t.Fatal(err)
			}
		}
	}
	factory := &discoveryCountingFactory{}
	svc := service.NewModelProviderServiceWithCatalog(pm, factory)
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.Use(gin.Recovery())
	routes := router.NewRouter(handler.NewAuthHandler(), nil, nil, nil, nil, handler.NewSystemHandler(service.NewSystemService()), nil, nil, nil, nil, nil, nil, nil, nil, handler.NewProviderHandler(nil, svc), nil)
	routes.Setup(app)
	api := discoveryMetadataServer(t, j, "application", app)
	snapshot := func() string {
		t.Helper()
		value := ""
		for _, table := range []string{"t_ai_users", "t_ai_user_tenants", "tenant_model_provider", "tenant_model_instance", "tenant_model"} {
			var hash string
			if err := read.QueryRow("SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY id)::text,'')) FROM " + table + " t").Scan(&hash); err != nil {
				t.Fatal(err)
			}
			value += hash
		}
		return value
	}
	before := snapshot()
	endpoint := func(provider, instance string) string {
		return "/api/v1/providers/" + provider + "/instances/" + instance + "/models?supported=true"
	}
	request := func(provider, instance, identity string) (int, []byte) {
		t.Helper()
		req, err := http.NewRequestWithContext(t.Context(), http.MethodGet, api.URL+endpoint(provider, instance), nil)
		if err != nil {
			t.Fatal(err)
		}
		if identity != "" {
			req.Header.Set("Authorization", "Bearer "+tokens[identity])
		}
		response, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		var output bytes.Buffer
		output.ReadFrom(response.Body)
		if snapshot() != before {
			t.Fatal("discovery mutated SQL")
		}
		return response.StatusCode, output.Bytes()
	}
	for _, provider := range []string{"Gitee", "OpenAI"} {
		for _, instance := range []string{"one", "two"} {
			status, body := request(provider, instance, "owner")
			var response struct {
				Code int                     `json:"code"`
				Data []entity.SupportedModel `json:"data"`
			}
			if status != 200 || json.Unmarshal(body, &response) != nil || response.Code != 0 || len(response.Data) != 3 {
				t.Fatalf("inventory status/code/cardinality: %d %s", status, body)
			}
			rows := response.Data
			if rows[0].Name != "namespace/raw" || rows[1].Name != "unknown" || rows[2].Name != "zero" || rows[0].MaxTokens == nil || *rows[0].MaxTokens != 8192 || rows[0].Dimension == nil || *rows[0].Dimension != 128 || rows[0].Thinking == nil || rows[0].Thinking.DefaultValue || rows[0].ModelTypes == nil || len(*rows[0].ModelTypes) != 2 || rows[1].MaxTokens != nil || rows[1].ModelTypes != nil || rows[2].MaxTokens == nil || *rows[2].MaxTokens != 0 || rows[2].ModelTypes == nil || len(*rows[2].ModelTypes) != 0 {
				t.Fatalf("typed metadata: %s", body)
			}
			if bytes.Contains(body, []byte("model_name")) || bytes.Contains(body, []byte("vendor")) || !bytes.Contains(body, []byte(`{"name":"unknown"}`)) {
				t.Fatalf("legacy/remote ID/unknown fields: %s", body)
			}
		}
		for _, instance := range []string{"bad", "business", "null"} {
			_, body := request(provider, instance, "owner")
			var result map[string]any
			if json.Unmarshal(body, &result) != nil || result["code"] == float64(0) || result["data"] != nil || bytes.Contains(body, []byte("private-provider-error")) {
				t.Fatalf("strict failure/no partial result: %s", body)
			}
		}
		_, body := request(provider, "empty", "owner")
		if !bytes.Contains(body, []byte(`"data":[]`)) {
			t.Fatalf("empty inventory: %s", body)
		}
	}
	for _, test := range []struct{ instance, user string }{{"one", ""}, {"one", "foreign"}, {"one", "member"}, {"missing", "owner"}, {"region", "owner"}, {"disabled", "owner"}, {"keyless", "owner"}} {
		remoteBefore, constructBefore := calls.Load(), factory.calls.Load()
		status, body := request("Gitee", test.instance, test.user)
		var result map[string]any
		_ = json.Unmarshal(body, &result)
		if result["code"] == float64(0) || calls.Load() != remoteBefore || factory.calls.Load() != constructBefore || test.user == "" && status != http.StatusUnauthorized {
			t.Fatalf("denial reached provider: %d %s", status, body)
		}
	}
	ctx, cancel := context.WithTimeout(t.Context(), 150*time.Millisecond)
	rows, err := svc.ListSupportedModelsWithContext(ctx, "Gitee", "slow", "owner")
	cancel()
	if !errors.Is(err, context.DeadlineExceeded) || rows != nil {
		t.Fatalf("timeout partial result: %v %v", rows, err)
	}
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("timeout did not start")
	}
	select {
	case <-cancelled:
	case <-time.After(time.Second):
		t.Fatal("timeout not propagated upstream")
	}
	ctx, cancel = context.WithCancel(t.Context())
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, api.URL+endpoint("Gitee", "slow"), nil)
	req.Header.Set("Authorization", "Bearer "+tokens["owner"])
	done := make(chan error, 1)
	go func() {
		resp, err := http.DefaultClient.Do(req)
		if resp != nil {
			resp.Body.Close()
		}
		done <- err
	}()
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("HTTP cancel did not start")
	}
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatal("HTTP cancel result", err)
	}
	select {
	case <-cancelled:
	case <-time.After(time.Second):
		t.Fatal("HTTP cancel not propagated upstream")
	}
	if got, err := svc.ListSupportedModelsWithContext(ctx, "Gitee", "one", "owner"); !errors.Is(err, context.Canceled) || got != nil {
		t.Fatal("pre-cancel result", got, err)
	}
	if snapshot() != before {
		t.Fatal("cancel/timeout mutated SQL")
	}
	if binary := os.Getenv("MULTIRAG_DISCOVERY_CLI"); binary != "" {
		content, err := os.ReadFile(binary)
		if err != nil {
			t.Fatal(err)
		}
		u, _ := url.Parse(api.URL)
		command := []string{"-h", u.Host, "-t", tokens["owner"], "-o", "json", `LIST SUPPORTED MODELS FROM "Gitee" "one";`}
		j.Event("reserve-cli-process", map[string]any{"binary": binary, "sha256": fmt.Sprintf("%x", sha256.Sum256(content)), "syntax": command[len(command)-1]})
		cmd := exec.CommandContext(t.Context(), binary, command...)
		cmd.Dir = t.TempDir()
		output, err := cmd.CombinedOutput()
		if err != nil || !bytes.Contains(output, []byte(`"name": "namespace/raw"`)) || !bytes.Contains(output, []byte(`"max_tokens": 0`)) || bytes.Contains(output, []byte("model_name")) {
			t.Fatalf("CLI discovery: %v %s", err, output)
		}
		j.Event("cli-process-exit", map[string]any{"pid": cmd.Process.Pid, "exit": cmd.ProcessState.ExitCode(), "typedMetadata": true})
	}
	j.Event("discovery-contract", map[string]any{"providers": 2, "knownUnknownZero": true, "strictFailures": true, "SQLUnchanged": snapshot() == before, "snapshotBefore": before, "snapshotAfter": snapshot(), "cancelTimeout": true, "cli": os.Getenv("MULTIRAG_DISCOVERY_CLI") != ""})
}
