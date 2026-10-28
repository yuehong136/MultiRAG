package service_test

import (
	"bytes"
	"crypto/rand"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/utility"
)

var dAfter53JournalMu sync.Mutex

func dAfter53Record(t *testing.T, value map[string]any) {
	t.Helper()
	dAfter53JournalMu.Lock()
	defer dAfter53JournalMu.Unlock()
	path := os.Getenv("MULTIRAG_PROVIDER_TEST_JOURNAL")
	if path == "" {
		t.Fatal("registered case journal required")
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	value["case"], value["parent_run_id"] = t.Name(), os.Getenv("MULTIRAG_RUN_ID")
	if err := json.NewEncoder(f).Encode(value); err != nil {
		t.Fatal(err)
	}
	if err := f.Sync(); err != nil {
		t.Fatal(err)
	}
}

func dAfter53Bind(t *testing.T, label string, h http.Handler) *httptest.Server {
	t.Helper()
	var token [2]byte
	if _, err := rand.Read(token[:]); err != nil {
		t.Fatal(err)
	}
	address := fmt.Sprintf("127.0.0.1:%d", 20000+(int(token[0])*256+int(token[1]))%30000)
	dAfter53Record(t, map[string]any{"action": "intent", "kind": "http-listener", "label": label, "address": address})
	listener, err := net.Listen("tcp", address)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
	srv := &httptest.Server{Listener: listener, Config: &http.Server{Handler: h}}
	t.Cleanup(func() {
		srv.CloseClientConnections()
		srv.Close()
		conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
		if conn != nil {
			conn.Close()
		}
		dAfter53Record(t, map[string]any{"action": "cleanup-readback", "kind": "http-listener", "label": label, "address": address, "absent": err != nil})
		if err == nil {
			t.Error("listener remains", address)
		}
	})
	srv.Start()
	return srv
}

// Real JWT HTTP and SQL bind a dynamically registered tenant model to the
// catalog's concrete GPUStack driver. The GPUStack wire service is controlled.
func TestDAfter53GPUStackHTTPAndSQL(t *testing.T) {
	config := os.Getenv("MULTIRAG_D_AFTER53_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned D config required")
		}
		t.Skip("requires owned D PostgreSQL configuration")
	}
	root := filepath.Join("..", "..")
	if _, err := os.Stat("go.mod"); err == nil {
		root = "."
	}
	t.Chdir(root)
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal("private config load failed")
	}
	cfg := server.GetConfig().Database
	if !strings.HasPrefix(cfg.Database, "multirag_after53_d_") || cfg.Host != "127.0.0.1" || cfg.Schema != "usr_ai" {
		t.Fatal("unowned database")
	}
	dAfter53Record(t, map[string]any{"action": "intent", "kind": "sql-schema", "database": cfg.Database, "schema": cfg.Schema, "scope": "dao.InitDB migrations in this exclusive scratch database"})
	if err := dao.InitDB(); err != nil {
		t.Fatal("owned schema init", err)
	}
	pool, err := dao.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	dsn, err := dao.PostgresDSN(cfg)
	if err != nil {
		t.Fatal(err)
	}
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { readback.Close() })
	tables := []string{"tenant_model", "tenant_model_instance", "tenant_model_provider", "t_ai_user_tenants", "t_ai_users"}
	for _, table := range tables {
		var n int
		if err := readback.QueryRow("SELECT count(*) FROM " + table).Scan(&n); err != nil || n != 0 {
			t.Fatal("scratch not empty", table, n, err)
		}
	}
	t.Cleanup(func() {
		for _, table := range tables {
			if _, err := readback.Exec("DELETE FROM " + table); err != nil {
				t.Error(err)
			}
			var n int
			err := readback.QueryRow("SELECT count(*) FROM " + table).Scan(&n)
			dAfter53Record(t, map[string]any{"action": "cleanup-readback", "kind": "sql-table", "database": cfg.Database, "table": table, "count": n, "absent": err == nil && n == 0})
			if err != nil || n != 0 {
				t.Error(table, n, err)
			}
		}
	})
	if err := server.InitVariables(nil); err != nil {
		t.Fatal(err)
	}
	local.InitAdminStatus(0, "owned D after53 acceptance")
	svc := service.NewModelProviderService()
	definition := dao.GetModelProviderManager().FindProvider("GPUStack")
	if definition == nil || definition.URLSuffix.Embedding != "v1-openai/embeddings" {
		t.Fatal("actual catalog missing embedding endpoint")
	}
	if _, ok := definition.ModelDriver.(*models.GPUStackModel); !ok {
		t.Fatal("catalog driver mismatch")
	}
	tokens := map[string]string{}
	active := "1"
	for _, id := range []string{"after53-d-owner", "after53-d-other"} {
		dAfter53Record(t, map[string]any{"action": "intent", "kind": "sql-identity", "database": cfg.Database, "user": id, "tenant": id + "-tenant"})
		email := id + "@owned.invalid"
		if err := dao.DB.Create(&entity.User{ID: id, Email: email, Nickname: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := dao.DB.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: id + "-tenant", Role: "owner", InvitedBy: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		token, err := utility.SignUserJWT(email, server.GetSecretKey())
		if err != nil {
			t.Fatal(err)
		}
		tokens[id] = token
	}
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.Use(gin.Recovery())
	routes := router.NewRouter(handler.NewAuthHandler(), nil, nil, nil, nil, handler.NewSystemHandler(service.NewSystemService()), nil, nil, nil, nil, nil, nil, nil, nil, nil, handler.NewProviderHandler(nil, svc), nil)
	routes.Setup(app)
	api := dAfter53Bind(t, "application", app)
	client := &http.Client{Timeout: 10 * time.Second}
	t.Cleanup(client.CloseIdleConnections)
	request := func(method, path, id string, body any) map[string]any {
		t.Helper()
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		req, err := http.NewRequestWithContext(t.Context(), method, api.URL+path, bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		if id != "" {
			req.Header.Set("Authorization", "Bearer "+tokens[id])
		}
		resp, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		raw, err = io.ReadAll(resp.Body)
		if err != nil {
			t.Fatal(err)
		}
		var result map[string]any
		if err := json.Unmarshal(raw, &result); err != nil {
			t.Fatal(resp.StatusCode, string(raw), err)
		}
		return result
	}
	success := func(method, path string, body any) map[string]any {
		t.Helper()
		result := request(method, path, "after53-d-owner", body)
		if result["code"] != float64(0) {
			t.Fatal(method, path, result)
		}
		return result
	}
	snapshot := func() string {
		t.Helper()
		result := ""
		for _, table := range tables {
			var digest string
			if err := readback.QueryRow("SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY id)::text,'')) FROM " + table + " t").Scan(&digest); err != nil {
				t.Fatal(err)
			}
			result += digest
		}
		return result
	}
	dAfter53Record(t, map[string]any{"action": "intent", "kind": "sql-provider", "database": cfg.Database, "provider": "GPUStack", "tenant": "after53-d-owner-tenant"})
	success("PUT", "/api/v1/providers/", map[string]any{"provider_name": "GPUStack"})
	var calls atomic.Int32
	for _, instance := range []string{"one", "two"} {
		key := "controlled-" + instance
		fixture := dAfter53Bind(t, instance, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls.Add(1)
			if r.URL.Path != "/instance/v1-openai/embeddings" || r.Header.Get("Authorization") != "Bearer "+key {
				t.Error("endpoint/instance auth crossed", r.URL)
			}
			var body struct {
				Model      string   `json:"model"`
				Input      []string `json:"input"`
				Dimensions int      `json:"dimensions"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Error(err)
				return
			}
			if body.Model != "tenant-embedding" || body.Dimensions != 2 || len(body.Input) == 0 || len(body.Input) > 32 {
				t.Error("model/batch/dimension contract", body)
			}
			data := []map[string]any{}
			bias := 100
			if instance == "two" {
				bias = 200
			}
			for i := len(body.Input) - 1; i >= 0; i-- {
				data = append(data, map[string]any{"index": i, "embedding": []float64{float64(bias + len(body.Input[i])), 0}})
			}
			json.NewEncoder(w).Encode(map[string]any{"data": data})
		}))
		base := fixture.URL + "/instance"
		if instance == "two" {
			base += "/v1/"
		}
		dAfter53Record(t, map[string]any{"action": "intent", "kind": "sql-instance-and-models", "database": cfg.Database, "provider": "GPUStack", "instance": instance, "models": []string{"tenant-embedding", "tenant-chat"}})
		success("POST", "/api/v1/providers/GPUStack/instances", map[string]any{"instance_name": instance, "api_key": key, "base_url": base, "region": "office"})
		for _, kind := range []string{"embedding", "chat"} {
			success("POST", "/api/v1/providers/GPUStack/instances/"+instance+"/models", map[string]any{"model_name": "tenant-" + kind, "model_type": kind, "max_tokens": 1024})
		}
		var storedType, storedExtra string
		if err := readback.QueryRow(`SELECT m.model_type,i.extra FROM tenant_model m JOIN tenant_model_instance i ON i.id=m.instance_id JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.tenant_id=$1 AND i.instance_name=$2 AND m.model_name='tenant-embedding'`, "after53-d-owner-tenant", instance).Scan(&storedType, &storedExtra); err != nil || storedType != "embedding" || !strings.Contains(storedExtra, base) {
			t.Fatal("committed SQL model/instance readback", storedType, err)
		}
	}
	before := snapshot()
	texts := make([]string, 33)
	for i := range texts {
		texts[i] = strings.Repeat("x", i+1)
	}
	for _, instance := range []string{"one", "two"} {
		bound, err := svc.GetEmbeddingModel("after53-d-owner-tenant", "tenant-embedding@"+instance+"@GPUStack")
		if err != nil {
			t.Fatal(err)
		}
		if _, ok := bound.ModelDriver.(*models.GPUStackModel); !ok {
			t.Fatal("resolved driver is not GPUStack")
		}
		bound.EmbeddingConfig = &models.EmbeddingConfig{Dimension: 2}
		v, err := bound.Embed(texts)
		if err != nil || len(v) != 33 || v[32].Index != 32 {
			t.Fatal(v, err)
		}
		bias := 100
		if instance == "two" {
			bias = 200
		}
		for i, item := range v {
			if item.Index != i || item.Embedding[0] != float64(bias+i+1) || item.Embedding[1] != 0 {
				t.Fatal(i, item)
			}
		}
		body := map[string]any{"provider_name": "GPUStack", "instance_name": instance, "model_name": "tenant-embedding", "texts": texts, "dimension": 2}
		result := success("POST", "/api/v1/embeddings", body)
		data, ok := result["data"].([]any)
		if !ok || len(data) != 33 {
			t.Fatal(result)
		}
		for _, id := range []string{"after53-d-other", ""} {
			n := calls.Load()
			result := request("POST", "/api/v1/embeddings", id, body)
			if result["code"] == float64(0) || calls.Load() != n || snapshot() != before {
				t.Fatal("auth denial effects", id, result)
			}
		}
		body["model_name"] = "tenant-chat"
		n := calls.Load()
		result = request("POST", "/api/v1/embeddings", "after53-d-owner", body)
		if result["code"] == float64(0) || calls.Load() != n || snapshot() != before {
			t.Fatal("chat-only capability bypass", result)
		}
	}
	if calls.Load() != 8 || snapshot() != before {
		t.Fatal("unexpected calls or inference SQL mutation", calls.Load())
	}
	t.Log("actual GPUStack catalog, two tenant instances, bound driver and authenticated embedding HTTP; 8 controlled calls, denial zero I/O and independent SQL digests")
}
