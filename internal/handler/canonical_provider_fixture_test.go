package handler_test

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"multirag/internal/cli"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/testsupport"
	"multirag/internal/testsupport/scratch"
	"multirag/internal/utility"
)

type canonicalProviderFixture struct {
	*scratch.Services
	svc                *service.ModelProviderService
	url                string
	client             *http.Client
	tokens             map[string]string
	modelMutationCalls atomic.Int64
}

func newCanonicalProviderFixture(t *testing.T) *canonicalProviderFixture {
	t.Helper()
	s := scratch.New(t)
	oldTransport := http.DefaultTransport
	http.DefaultTransport = providerLoopbackTransport{underlying: oldTransport}
	t.Cleanup(func() { http.DefaultTransport = oldTransport })
	_, file, _, _ := runtime.Caller(0)
	t.Chdir(filepath.Join(filepath.Dir(file), "..", ".."))
	config := filepath.Join(filepath.Dir(os.Getenv("MULTIRAG_GO_ALIGNMENT_CONFIG")), "service_conf.yaml")
	if err := server.Init(config); err != nil {
		t.Fatal("initialize private canonical config")
	}
	server.GetConfig().Database = s.Config.Database
	previous := dao.DB
	if err := dao.InitDB(); err != nil {
		t.Fatal(err)
	}
	pool, err := dao.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close(); dao.DB = previous })
	previousGate := local.GetAdminStatus()
	local.SetAdminStatus(0, "controlled owned provider fixture")
	t.Cleanup(func() { local.SetAdminStatus(previousGate.Status, previousGate.Reason) })
	secret := s.Prefix + "-private-provider-signature-secret"
	server.GetConfig().Server.SecretKey = &secret
	f := &canonicalProviderFixture{Services: s, svc: service.NewModelProviderService(), tokens: map[string]string{}}
	active := "1"
	for _, who := range []string{"owner", "foreign", "inactive", "superuser", "revoked", "invited", "member", "disabledtenant"} {
		access := utility.GenerateToken()
		isActive := "1"
		superuser := who == "superuser"
		if who == "inactive" {
			isActive = "0"
		}
		if err := dao.DB.Create(&entity.User{ID: who, Email: who + "@fixture.invalid", Nickname: who, AccessToken: &access, Status: &active, IsActive: isActive, IsSuperuser: &superuser}).Error; err != nil {
			t.Fatal(err)
		}
		tenantStatus := active
		if who == "disabledtenant" {
			tenantStatus = "0"
		}
		if err := dao.DB.Create(&entity.Tenant{ID: who + "-tenant", Status: &tenantStatus}).Error; err != nil {
			t.Fatal(err)
		}
		role := "owner"
		relationStatus := active
		tenantID := who + "-tenant"
		if who == "revoked" {
			relationStatus = "0"
		}
		if who == "invited" {
			role = "invite"
		}
		if who == "member" {
			role = "normal"
			tenantID = "owner-tenant"
		}
		if err := dao.DB.Create(&entity.UserTenant{ID: who, UserID: who, TenantID: tenantID, Role: role, InvitedBy: who, Status: &relationStatus}).Error; err != nil {
			t.Fatal(err)
		}
		token, err := utility.DumpAccessToken(access, secret)
		if err != nil {
			t.Fatal(err)
		}
		f.tokens[who] = token
	}
	gin.SetMode(gin.TestMode)
	app := gin.New()
	app.Use(gin.Recovery())
	app.Use(func(c *gin.Context) {
		if c.Request.Method == "POST" && strings.HasSuffix(c.Request.URL.Path, "/models") {
			f.modelMutationCalls.Add(1)
		}
		c.Next()
	})
	h := handler.NewProviderHandler(nil, f.svc)

	// Use the exact canonical provider paths and actual authorization middleware.
	// The unrelated runtime/admin startup gate belongs to root's standalone server smoke.
	v1 := app.Group("/api/v1", handler.NewAuthHandler().AuthMiddleware())
	p := v1.Group("/providers")
	p.GET("/", h.ListProviders)
	p.PUT("/", h.AddProvider)
	p.GET("/:provider_name", h.ShowProvider)
	p.DELETE("/:provider_name", h.DeleteProvider)
	p.GET("/:provider_name/models", h.ListModels)
	p.GET("/:provider_name/models/:model_name", h.ShowModel)
	p.POST("/:provider_name/instances", h.CreateProviderInstance)
	p.GET("/:provider_name/instances", h.ListProviderInstances)
	p.GET("/:provider_name/instances/:instance_name", h.ShowProviderInstance)
	p.GET("/:provider_name/instances/:instance_name/balance", h.ShowInstanceBalance)
	p.GET("/:provider_name/instances/:instance_name/connection", h.CheckInstanceConnection)
	p.POST("/:provider_name/connection", h.CheckConnection)
	p.GET("/:provider_name/instances/:instance_name/tasks", h.ListTasks)
	p.GET("/:provider_name/instances/:instance_name/tasks/:task_id", h.ShowTask)
	p.PUT("/:provider_name/instances/:instance_name", h.AlterProviderInstance)
	p.DELETE("/:provider_name/instances", h.DropProviderInstance)
	p.GET("/:provider_name/instances/:instance_name/models", h.ListInstanceModels)
	p.PATCH("/:provider_name/instances/:instance_name/models/*model_name", h.EnableOrDisableModel)
	p.POST("/:provider_name/instances/:instance_name/models", h.AddModel)
	p.DELETE("/:provider_name/instances/:instance_name/models", h.DropInstanceModels)
	v1.POST("/chat/completions", h.ChatToModel)
	v1.POST("/embeddings", h.EmbedText)
	v1.POST("/rerank", h.RerankDocument)
	v1.POST("/audio/transcriptions", h.TranscribeAudio)
	v1.POST("/audio/speech", h.AudioSpeech)
	v1.POST("/file/ocr", h.OCRFile)
	v1.POST("/file/parse", h.ParseFile)

	v1.GET("/users/me", handler.NewUserHandler(service.NewUserService()).Info)
	chat := handler.NewChatSessionHandler(service.NewChatSessionService(), service.NewUserService())
	app.POST("/v1/conversation/completion", handler.NewAuthHandler().AuthMiddleware(), func(c *gin.Context) {
		_ = http.NewResponseController(c.Writer).SetWriteDeadline(time.Now().Add(40 * time.Millisecond))
		chat.Completion(c)
	})
	api := testsupport.NewHTTP(t, app)
	f.url = api.URL
	f.client = api.Client()
	return f
}
func (f *canonicalProviderFixture) request(t *testing.T, who, method, path string, body any) (int, map[string]any, string) {
	t.Helper()
	var data []byte
	var err error
	if body != nil {
		data, err = json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
	}
	req, err := http.NewRequest(method, f.url+path, bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	if who != "" {
		req.Header.Set("Authorization", "Bearer "+f.tokens[who])
	}
	res, err := f.client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	raw, err := io.ReadAll(res.Body)
	if err != nil {
		t.Fatal(err)
	}
	result := map[string]any{}
	if !strings.Contains(res.Header.Get("Content-Type"), "text/event-stream") {
		if err := json.Unmarshal(raw, &result); err != nil {
			t.Fatalf("non-JSON HTTP status %d", res.StatusCode)
		}
	}
	return res.StatusCode, result, string(raw)
}
func (f *canonicalProviderFixture) requireSuccess(t *testing.T, who, method, path string, body any) map[string]any {
	t.Helper()
	status, result, _ := f.request(t, who, method, path, body)
	if status != 200 || result["code"] != float64(0) {
		t.Fatalf("%s %s failed: HTTP %d code=%v message=%v", method, path, status, result["code"], result["message"])
	}
	return result
}
func (f *canonicalProviderFixture) enroll(t *testing.T, provider, instance, key, baseURL, model, kind string) string {
	t.Helper()
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM tenant_model_provider WHERE tenant_id=? AND provider_name=?", "owner-tenant", provider).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count == 0 {
		f.requireSuccess(t, "owner", "PUT", "/api/v1/providers/", map[string]any{"provider_name": provider})
	}
	var instanceCount int
	if err := f.Read.QueryRow("SELECT count(*) FROM tenant_model_instance i JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.tenant_id=? AND p.provider_name=? AND i.instance_name=?", "owner-tenant", provider, instance).Scan(&instanceCount); err != nil {
		t.Fatal(err)
	}
	path := "/api/v1/providers/" + url.PathEscape(provider) + "/instances"
	if instanceCount == 0 {
		f.requireSuccess(t, "owner", "POST", path, map[string]any{"instance_name": instance, "api_key": key, "base_url": baseURL, "region": "default"})
	}
	f.requireSuccess(t, "owner", "POST", path+"/"+instance+"/models", map[string]any{"models": []any{map[string]any{"model_name": model, "model_types": []string{kind}, "max_tokens": 128}}})
	var id string
	if err := f.Read.QueryRow("SELECT m.id FROM tenant_model m JOIN tenant_model_instance i ON i.id=m.instance_id JOIN tenant_model_provider p ON p.id=m.provider_id WHERE p.tenant_id=? AND p.provider_name=? AND i.instance_name=? AND m.model_name=?", "owner-tenant", provider, instance, model).Scan(&id); err != nil || len(id) != 32 {
		t.Fatal("canonical UUID model row absent", err)
	}
	return id
}
func (f *canonicalProviderFixture) counts(t *testing.T) [4]int {
	t.Helper()
	var counts [4]int
	for i, table := range []string{"tenant_model_provider", "tenant_model_instance", "tenant_model", "tenant_model_group_mapping"} {
		if err := f.Read.QueryRow("SELECT count(*) FROM " + table).Scan(&counts[i]); err != nil {
			t.Fatal(err)
		}
	}
	return counts
}
func (f *canonicalProviderFixture) cli(t *testing.T, who string) *cli.CLI {
	t.Helper()
	address, err := url.Parse(f.url)
	if err != nil {
		t.Fatal(err)
	}
	h := cli.NewHTTPClient()
	h.Host = address.Hostname()
	h.Port, _ = strconv.Atoi(address.Port())
	token := f.tokens[who]
	h.LoginToken = &token
	return &cli.CLI{Config: &cli.CommandLineConfig{CLIMode: cli.APIMode, APIClientConfig: cli.APIModeConfig{CurrentAPIServer: "fixture", APIServerMap: map[string]*cli.APIServerConfig{"fixture": {Name: "fixture"}}}}, APIServerClientMap: map[string]*cli.HTTPClient{"fixture": h}}
}
func providerEnvelopeError(result map[string]any) error {
	if result["code"] == float64(0) {
		return nil
	}
	return fmt.Errorf("provider envelope error code %v", result["code"])
}

// Snapshot through a separate SQL connection; hash private cells without logging them.
func (f *canonicalProviderFixture) snapshot(t *testing.T) string {
	t.Helper()
	var result []any
	for _, table := range []string{"user", "tenant", "user_tenant", "tenant_model_provider", "tenant_model_instance", "tenant_model", "tenant_model_group_mapping"} {
		order := "id"
		if table == "tenant_model_group_mapping" {
			order = "group_id,provider_id,instance_id,model_id"
		}
		rows, err := f.Read.Query("SELECT * FROM " + table + " ORDER BY " + order)
		if err != nil {
			t.Fatal(err)
		}
		columns, err := rows.Columns()
		if err != nil {
			t.Fatal(err)
		}
		var records []any
		for rows.Next() {
			values := make([]any, len(columns))
			pointers := make([]any, len(values))
			for i := range values {
				pointers[i] = &values[i]
			}
			if err := rows.Scan(pointers...); err != nil {
				t.Fatal(err)
			}
			for i, value := range values {
				if raw, ok := value.([]byte); ok {
					values[i] = string(raw)
				}
			}
			records = append(records, values)
		}
		if err := rows.Err(); err != nil {
			t.Fatal(err)
		}
		rows.Close()
		result = append(result, []any{table, records})
	}
	raw, err := json.Marshal(result)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(raw)
	return hex.EncodeToString(digest[:])
}

type providerLoopbackTransport struct{ underlying http.RoundTripper }

func (t providerLoopbackTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	host := r.URL.Hostname()
	ip := net.ParseIP(host)
	if host != "localhost" && (ip == nil || !ip.IsLoopback()) {
		return nil, errors.New("non-loopback provider HTTP blocked")
	}
	return t.underlying.RoundTrip(r)
}
