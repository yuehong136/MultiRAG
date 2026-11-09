package handler

import (
	modelModule "multirag/internal/entity/models"

	"context"
	"crypto/rand"
	"database/sql"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"io"
	"multirag/internal/cli"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/service"
	"multirag/internal/testsupport"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

func modelIDScratchDB(t *testing.T) (*gorm.DB, *sql.DB, *testsupport.CaseJournal) {
	t.Helper()
	path := os.Getenv("MULTIRAG_MODEL_ID_CONFIG")
	if path == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("private model ID scratch config required")
		}
		t.Skip("requires owned PostgreSQL for model ID consumers")
	}
	journal := testsupport.NewCase(t)
	var cfg struct {
		AdminDSN   string `json:"admin_dsn"`
		PGEndpoint string `json:"pg_endpoint"`
	}
	raw, err := os.ReadFile(path)
	if err != nil || json.Unmarshal(raw, &cfg) != nil {
		t.Fatal("private scratch config unavailable")
	}
	u, err := url.Parse(cfg.AdminDSN)
	if err != nil || u.Host != cfg.PGEndpoint || u.Hostname() != "127.0.0.1" || u.Path != "/postgres" {
		t.Fatal("explicit local admin endpoint required")
	}
	admin, err := sql.Open("pgx", cfg.AdminDSN)
	if err != nil {
		t.Fatal("open admin connection")
	}
	t.Cleanup(func() { admin.Close() })
	name := "multirag_after22_m_" + strings.ReplaceAll(uuid.NewString(), "-", "")[:16]
	journal.Event("reserve-database", map[string]any{"endpoint": u.Host, "database": name, "config_file": path})
	var present bool
	if err = admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&present); err != nil || present {
		t.Fatal("scratch absence precondition")
	}
	t.Cleanup(func() {
		_, dropErr := admin.Exec(`DROP DATABASE IF EXISTS "` + name + `" WITH (FORCE)`)
		readErr := admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&present)
		journal.Event("cleanup-database-readback", map[string]any{"database": name, "absent": !present, "drop_ok": dropErr == nil, "read_ok": readErr == nil})
		if dropErr != nil || readErr != nil || present {
			t.Error("scratch database cleanup failed")
		}
	})
	if _, err = admin.Exec(`CREATE DATABASE "` + name + `"`); err != nil {
		t.Fatal("create scratch database", err)
	}
	u.Path = "/" + name
	db, err := gorm.Open(postgres.Open(u.String()), &gorm.Config{Logger: logger.Discard})
	if err != nil {
		t.Fatal("connect scratch database")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	read, err := sql.Open("pgx", u.String())
	if err != nil {
		t.Fatal("independent SQL reader")
	}
	t.Cleanup(func() { read.Close() })
	previous := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = previous })
	return db, read, journal
}

func TestModelIDConsumersHTTPCLIAndSQL(t *testing.T) {
	db, read, j := modelIDScratchDB(t)
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := db.AutoMigrate(&entity.Tenant{}, &entity.UserTenant{}, &entity.TenantLLM{}, &entity.TenantModelProvider{}, &entity.TenantModelInstance{}, &entity.TenantModel{}); err != nil {
		t.Fatal(err)
	}
	active := "1"
	chat := "chat"
	embedding := "embedding"
	rerank := "rerank"
	name := "gpt-4o"
	key := "numeric-key"
	for _, id := range []string{"owner", "other"} {
		if err := db.Create(&entity.Tenant{ID: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: id, Role: "owner", Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	var calls atomic.Int32
	fixture := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("Authorization") != "Bearer numeric-key" {
			t.Error("saved credential lost")
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		switch r.URL.Path {
		case "/v1/chat/completions":
			if body["model"] != "gpt-4o" {
				t.Error("selected model changed", body["model"])
			}
			if body["stream"] == true {
				w.Header().Set("Content-Type", "text/event-stream")
				time.Sleep(250 * time.Millisecond)
				fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
				return
			}
			fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
		case "/v1/embeddings":
			fmt.Fprint(w, `{"data":[{"index":0,"embedding":[0,1]}]}`)
		case "/v1/rerank":
			fmt.Fprint(w, `{"results":[{"index":0,"relevance_score":0}]}`)
		default:
			t.Error("unexpected provider path", r.URL.Path)
			w.WriteHeader(404)
		}
	}))
	base := fixture.URL + "/v1"
	rows := []entity.TenantLLM{{ID: 101, TenantID: "owner", LLMFactory: "OpenAI", LLMName: &name, ModelType: &chat, APIKey: &key, APIBase: &base, Status: "1"}, {ID: 102, TenantID: "other", LLMFactory: "OpenAI", LLMName: &name, ModelType: &chat, APIKey: &key, APIBase: &base, Status: "1"}, {ID: 103, TenantID: "owner", LLMFactory: "OpenAI", LLMName: &name, ModelType: &chat, APIKey: &key, APIBase: &base, Status: "0"}, {ID: 104, TenantID: "owner", LLMFactory: "OpenAI", LLMName: ptrString("embedding"), ModelType: &embedding, APIKey: &key, APIBase: &base, Status: "1"}, {ID: 105, TenantID: "owner", LLMFactory: "ZHIPU-AI", LLMName: ptrString("rerank"), ModelType: &rerank, APIKey: &key, APIBase: &base, Status: "1"}}
	// The legacy schema has a unique tenant/provider/name identity.
	rows[2].LLMName = ptrString("disabled")
	for i := range rows {
		if err := db.Create(&rows[i]).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := db.Create(&entity.TenantModelProvider{ID: "modern-provider", TenantID: "owner", ProviderName: "vllm"}).Error; err != nil {
		t.Fatal(err)
	}
	modernExtra := `{"region":"default","base_url":"` + base + `"}`
	if err := db.Create(&entity.TenantModelInstance{ID: "modern-instance", ProviderID: "modern-provider", InstanceName: "modern", Status: "active", APIKey: "", Extra: modernExtra}).Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Create(&entity.TenantModel{ID: "modern-model", ProviderID: "modern-provider", InstanceID: "modern-instance", ModelName: "modern-chat", ModelType: "chat", Status: "active", Extra: `{"max_tokens":0,"model_types":["chat"]}`}).Error; err != nil {
		t.Fatal(err)
	}
	local := entity.TenantLLM{ID: 106, TenantID: "owner", LLMFactory: "Ollama", LLMName: ptrString("local-chat"), ModelType: &chat, APIBase: &base, Status: "1"}
	if err := db.Create(&local).Error; err != nil {
		t.Fatal(err)
	}
	manager, err := modelModule.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	svc := service.NewModelProviderServiceWithCatalog(manager, nil)
	h := NewProviderHandler(nil, svc)
	tenant := NewTenantHandler(service.NewTenantServiceWithModels(svc), nil)
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	router.Use(func(c *gin.Context) {
		id := c.GetHeader("X-User")
		if id == "" && c.GetHeader("Authorization") == "Bearer controlled" {
			id = "owner"
		}
		if id != "" {
			c.Set("user", &entity.User{ID: id})
		}
		c.Next()
	})
	router.POST("/api/v1/chat/completions", h.ChatToModel)
	router.POST("/api/v1/embeddings", h.EmbedText)
	router.POST("/api/v1/rerank", h.RerankDocument)
	router.POST("/api/v1/audio/speech", h.AudioSpeech)
	router.POST("/api/v1/audio/transcriptions", h.TranscribeAudio)
	router.GET("/api/v1/models", h.ListTenantModels)
	router.PATCH("/api/v1/models/default", tenant.SetModels)
	router.GET("/api/v1/models/default", tenant.GetModels)
	api := modelIDHTTPServer(t, router, 100*time.Millisecond)
	request := func(method, path, body, user string) (int, string) {
		t.Helper()
		req, _ := http.NewRequest(method, api.URL+path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-User", user)
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		data, err := io.ReadAll(res.Body)
		if err != nil {
			t.Fatal(err)
		}
		return res.StatusCode, string(data)
	}
	for _, id := range []int{0, -1, 102, 103, 104, 999} {
		before := calls.Load()
		_, data := request("POST", "/api/v1/chat/completions", fmt.Sprintf(`{"model_id":%d,"message":"hello","stream":true}`, id), "owner")
		if strings.Contains(data, "event:") || strings.Contains(data, `"code":0`) || calls.Load() != before {
			t.Fatalf("invalid ID crossed provider or SSE boundary: %d %s", id, data)
		}
	}
	for _, path := range []string{"/api/v1/audio/speech", "/api/v1/audio/transcriptions"} {
		before := calls.Load()
		_, data := request("POST", path, `{"model_id":102,"text":"hello","file":"YQ==","stream":true}`, "owner")
		if strings.Contains(data, "event:") || strings.Contains(data, `"code":0`) || calls.Load() != before {
			t.Fatal("media opened SSE before authorization", data)
		}
	}
	for _, body := range []string{`{"model_id":101,"provider_name":"OpenAI","message":"hi"}`, `{"model_id":"101","message":"hi"}`, `{"model_id":101.5,"message":"hi"}`, `{"model_id":9223372036854775808,"message":"hi"}`} {
		before := calls.Load()
		status, _ := request("POST", "/api/v1/chat/completions", body, "owner")
		if status != 400 || calls.Load() != before {
			t.Fatal("ambiguous numeric selector accepted")
		}
	}
	_, data := request("POST", "/api/v1/chat/completions", `{"model_id":101,"messages":[{"role":"system","content":"system"},{"role":"user","content":"hello"}]}`, "owner")
	if !strings.Contains(data, `"answer":"answer"`) {
		t.Fatal(data)
	}
	_, data = request("POST", "/api/v1/chat/completions", `{"model_id":101,"message":"hello","stream":true}`, "owner")
	if !strings.Contains(data, "[MESSAGE]answer") || !strings.Contains(data, "[DONE]") {
		t.Fatal(data)
	}
	_, data = request("GET", "/api/v1/models", "", "owner")
	if strings.Contains(data, "numeric-key") || strings.Contains(data, fixture.URL) || strings.Contains(data, `"id":102`) || !strings.Contains(data, `"id":101`) {
		t.Fatal("credential-free inventory failed", data)
	}
	_, data = request("PATCH", "/api/v1/models/default", `{"model_id":101,"model_type":"chat"}`, "owner")
	if !strings.Contains(data, `"code":0`) {
		t.Fatal(data)
	}
	var selected int64
	if err := read.QueryRow("SELECT tenant_llm_id FROM t_ai_tenants WHERE id='owner'").Scan(&selected); err != nil || selected != 101 {
		t.Fatal("independent default readback", err, selected)
	}
	_, data = request("PATCH", "/api/v1/models/default", `{"model_id":102,"model_type":"chat"}`, "owner")
	if strings.Contains(data, `"code":0`) {
		t.Fatal("cross-tenant default accepted")
	}
	if err := read.QueryRow("SELECT tenant_llm_id FROM t_ai_tenants WHERE id='owner'").Scan(&selected); err != nil || selected != 101 {
		t.Fatal("failed default changed committed row")
	}
	_, data = request("GET", "/api/v1/models/default", "", "owner")
	if !strings.Contains(data, `"model_id":101`) {
		t.Fatal("numeric default not read back", data)
	}
	// Actual CLI formatter/client pipeline uses numeric JSON without float conversion.
	client := cli.NewMultiRAGClient("user")
	u, _ := url.Parse(api.URL)
	var port int
	fmt.Sscan(u.Port(), &port)
	client.HTTPClient.Host = u.Hostname()
	client.HTTPClient.Port = port
	client.HTTPClient.APIKey = "controlled"
	for _, input := range []string{`CHAT WITH '101' MESSAGE 'hello';`, `EMBED TEXT 'hi' WITH '104' DIMENSION 2;`, `RERANKER QUERY 'hi' DOCUMENT 'one' WITH '105' TOP 0;`, `SET DEFAULT LLM '101';`, `LIST DEFAULT MODELS;`} {
		cmd, err := cli.NewParser(input).Parse(false)
		if err != nil {
			t.Fatal(input, err)
		}
		result, err := client.ExecuteCommand(cmd)
		if err != nil || result == nil {
			t.Fatal("CLI execution", input, err)
		}
	}
	if bound, err := svc.GetChatModelByIDWithContext(context.Background(), "owner", 106); err != nil || bound == nil {
		t.Fatal("keyless local numeric binding", err)
	}
	_, data = request("GET", "/api/v1/models?type=chat", "", "owner")
	if !strings.Contains(data, "modern-chat") || strings.Contains(data, `"id":104`) {
		t.Fatal("typed modern/numeric inventory", data)
	}
	_, data = request("PATCH", "/api/v1/models/default", `{"model_type":"chat","model_provider":"vllm","model_instance":"modern","model_name":"modern-chat"}`, "owner")
	if !strings.Contains(data, `"code":0`) {
		t.Fatal("name default failed", data)
	}
	var numericDefault sql.NullInt64
	var namedDefault string
	if err := read.QueryRow("SELECT tenant_llm_id,llm_id FROM t_ai_tenants WHERE id='owner'").Scan(&numericDefault, &namedDefault); err != nil || numericDefault.Valid || namedDefault != "modern-chat@modern@vllm" {
		t.Fatal("numeric default shadowed name update", err, numericDefault, namedDefault)
	}
	before := calls.Load()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := svc.GetChatModelByIDWithContext(ctx, "owner", 101); err == nil || calls.Load() != before {
		t.Fatal("cancelled resolution performed IO")
	}
	j.Event("independent-default-readback", map[string]any{"id": selected, "denied_side_effects": false})
}
func ptrString(value string) *string { return &value }

func modelIDHTTPServer(t *testing.T, handler http.Handler, timeout time.Duration) *httptest.Server {
	t.Helper()
	journal := testsupport.NewCase(t)
	var listener net.Listener
	var address string
	for attempt := 0; attempt < 30; attempt++ {
		var random [2]byte
		if _, err := rand.Read(random[:]); err != nil {
			t.Fatal(err)
		}
		address = fmt.Sprintf("127.0.0.1:%d", 30000+int(binary.BigEndian.Uint16(random[:]))%20000)
		journal.Event("reserve-listener", map[string]any{"address": address, "pid": os.Getpid(), "pgid": syscall.Getpgrp()})
		var err error
		listener, err = net.Listen("tcp", address)
		if err == nil {
			break
		}
		journal.Event("listener-bind-failed", map[string]any{"address": address})
	}
	if listener == nil {
		t.Fatal("cannot bind registered HTTP listener")
	}
	server := &httptest.Server{Listener: listener, Config: &http.Server{Handler: handler, WriteTimeout: timeout}}
	t.Cleanup(func() {
		server.Close()
		conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
		if err == nil {
			conn.Close()
			t.Error("listener survived cleanup")
		}
		journal.Event("listener-cleanup-readback", map[string]any{"address": address, "absent": err != nil})
	})
	server.Start()
	server.Client().Timeout = 3 * time.Second
	journal.Event("listener-started", map[string]any{"address": address, "pid": os.Getpid(), "pgid": syscall.Getpgrp(), "write_timeout": timeout.String()})
	return server
}
