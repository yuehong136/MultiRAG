package handler_test

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/cli"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/service"
)

// Controlled identities and providers, real HTTP and independently read PostgreSQL.
func TestModelInferenceLiveHTTPAndSQL(t *testing.T) {
	config := os.Getenv("MULTIRAG_INFERENCE_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned inference scratch config required")
		}
		t.Skip("requires owned inference scratch database")
	}
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal("load private config failed")
	}
	if !strings.HasPrefix(server.GetConfig().Database.Database, "multirag_inference_") {
		t.Fatal("requires owned database")
	}
	if err := dao.InitDB(); err != nil {
		t.Fatal("initialize scratch failed", err)
	}
	pool, _ := dao.DB.DB()
	defer pool.Close()
	dsn, err := dao.PostgresDSN(server.GetConfig().Database)
	if err != nil {
		t.Fatal(err)
	}
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("independent readback failed")
	}
	defer readback.Close()
	active := "1"
	for _, id := range []string{"owner", "other"} {
		if err := dao.DB.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: id + "-tenant", Role: "owner", InvitedBy: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	var calls atomic.Int32
	fixtures := make([]*httptest.Server, 2)
	for i := range fixtures {
		fixtures[i] = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls.Add(1)
			if r.Header.Get("Authorization") != fmt.Sprintf("Bearer controlled-%d", i) {
				t.Error("credential crossed instances")
			}
			var body map[string]any
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Error(err)
			}
			switch r.URL.Path {
			case "/v4/embeddings":
				if body["dimensions"] != float64(2) {
					t.Error("dimension missing", body)
				}
				if body["input"] == "bad" {
					fmt.Fprint(w, `{"data":[{"embedding":[]}]}`)
				} else {
					fmt.Fprintf(w, `{"data":[{"embedding":[%d,2]}]}`, i+1)
				}
			case "/v4/rerank":
				if body["query"] == "bad" {
					fmt.Fprint(w, `{"results":[{"index":0,"relevance_score":1},{"index":0,"relevance_score":0}]}`)
					return
				}
				if body["query"] == "provider-error" {
					fmt.Fprint(w, `{"error":"private body"}`)
					return
				}
				top := int(body["top_n"].(float64))
				if top == 1 {
					fmt.Fprint(w, `{"results":[{"index":1,"relevance_score":0}]}`)
				} else {
					fmt.Fprint(w, `{"results":[{"index":1,"relevance_score":0},{"index":0,"relevance_score":0.7}]}`)
				}
			default:
				t.Error(r.URL.Path)
				w.WriteHeader(404)
			}
		}))
		defer fixtures[i].Close()
	}
	if err := dao.DB.Create(&entity.TenantModelProvider{ID: "provider", ProviderName: "ZHIPU-AI", TenantID: "owner-tenant"}).Error; err != nil {
		t.Fatal(err)
	}
	for i := range fixtures {
		extra, _ := json.Marshal(map[string]string{"region": "office", "base_url": fixtures[i].URL + "/v4"})
		if err := dao.DB.Create(&entity.TenantModelInstance{ID: fmt.Sprint(i), ProviderID: "provider", InstanceName: fmt.Sprint(i), APIKey: fmt.Sprintf("controlled-%d", i), Status: "active", Extra: string(extra)}).Error; err != nil {
			t.Fatal(err)
		}
	}
	h := handler.NewProviderHandler(nil, service.NewModelProviderService())
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	router.Use(func(c *gin.Context) {
		identity := strings.TrimPrefix(c.GetHeader("Authorization"), "Bearer ")
		if identity == "owner" || identity == "other" {
			c.Set("user", &entity.User{ID: identity})
		}
		c.Next()
	})
	router.POST("/api/v1/embeddings", h.EmbedText)
	router.POST("/api/v1/rerank", h.RerankDocument)
	api := httptest.NewServer(router)
	defer api.Close()
	parsed, _ := url.Parse(api.URL)
	port, _ := strconv.Atoi(parsed.Port())
	snapshot := func() string {
		t.Helper()
		var value string
		err := readback.QueryRow(`SELECT (SELECT count(*) FROM tenant_model_provider)::text || ':' || (SELECT count(*) FROM tenant_model_instance)::text || ':' || (SELECT count(*) FROM tenant_model)::text || ':' || (SELECT count(*) FROM t_ai_user_tenants)::text`).Scan(&value)
		if err != nil {
			t.Fatal("readback failed", err)
		}
		return value
	}
	before := snapshot()
	for i := range fixtures {
		httpClient := cli.NewHTTPClient()
		httpClient.Host = parsed.Hostname()
		httpClient.Port = port
		httpClient.APIKey = "owner"
		client := cli.NewMultiRAGClient("user")
		client.HTTPClient = httpClient
		for _, command := range []string{fmt.Sprintf("embed text 'a' 'b' with 'embedding-3@%d@ZHIPU-AI' dimension 2;", i), fmt.Sprintf("rerank query 'q' document 'a' 'b' with 'rerank@%d@ZHIPU-AI' top 1;", i)} {
			cmd, err := cli.NewParser(command).Parse(false)
			if err != nil {
				t.Fatal(err)
			}
			result, err := client.ExecuteUserCommand(cmd)
			if err != nil {
				t.Fatal(err)
			}
			rows := result.(*cli.CommonResponse).Data
			if strings.HasPrefix(command, "embed") {
				if len(rows) != 2 || rows[1]["index"] != float64(1) || rows[0]["dimension"] != float64(2) {
					t.Fatal(rows)
				}
			} else if len(rows) != 1 || rows[0]["index"] != float64(1) || rows[0]["relevance_score"] != float64(0) {
				t.Fatal(rows)
			}
		}
	}
	request := func(path, identity string, body map[string]any) (int, map[string]any) {
		t.Helper()
		raw, _ := json.Marshal(body)
		req, _ := http.NewRequest("POST", api.URL+path, bytes.NewReader(raw))
		req.Header.Set("Content-Type", "application/json")
		if identity != "" {
			req.Header.Set("Authorization", "Bearer "+identity)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var data map[string]any
		if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
			t.Fatal(err)
		}
		return resp.StatusCode, data
	}
	base := map[string]any{"provider_name": "ZHIPU-AI", "instance_name": "0", "model_name": "rerank", "query": "q", "documents": []string{"a", "b"}, "top_n": 0}
	for _, identity := range []string{"", "other"} {
		n := calls.Load()
		status, data := request("/api/v1/rerank", identity, base)
		if data["code"] == float64(0) || calls.Load() != n || identity == "" && status != 401 {
			t.Fatal("denial reached provider", status, data)
		}
	}
	for _, query := range []string{"bad", "provider-error"} {
		base["query"] = query
		_, data := request("/api/v1/rerank", "owner", base)
		if data["code"] == float64(0) {
			t.Fatal("malformed provider accepted", data)
		}
	}
	base["query"] = "q"
	base["top_n"] = -1
	n := calls.Load()
	_, data := request("/api/v1/rerank", "owner", base)
	if data["code"] == float64(0) || calls.Load() != n {
		t.Fatal("invalid top accepted")
	}
	_, data = request("/api/v1/embeddings", "owner", map[string]any{"provider_name": "ZHIPU-AI", "instance_name": "0", "model_name": "embedding-3", "texts": []string{"bad"}, "dimension": 2})
	if data["code"] == float64(0) {
		t.Fatal("empty vector accepted")
	}
	// A disabled instance and a model capability mismatch must make zero provider calls.
	if err := dao.DB.Model(&entity.TenantModelInstance{}).Where("id = ?", "0").Update("status", "inactive").Error; err != nil {
		t.Fatal(err)
	}
	n = calls.Load()
	base["top_n"] = 0
	_, data = request("/api/v1/rerank", "owner", base)
	if data["code"] == float64(0) || calls.Load() != n {
		t.Fatal("disabled instance reached provider")
	}
	if err := dao.DB.Model(&entity.TenantModelInstance{}).Where("id = ?", "0").Update("status", "active").Error; err != nil {
		t.Fatal(err)
	}
	base["model_name"] = "embedding-3"
	_, data = request("/api/v1/rerank", "owner", base)
	if data["code"] == float64(0) || calls.Load() != n {
		t.Fatal("capability mismatch reached provider")
	}
	if after := snapshot(); after != before {
		t.Fatal("inference changed SQL state", before, after)
	}
	var statuses int
	if err := readback.QueryRow(`SELECT count(*) FROM tenant_model_instance WHERE status='active'`).Scan(&statuses); err != nil || statuses != 2 {
		t.Fatal("independent status readback", statuses, err)
	}
	t.Logf("CLI parse/execute -> real HTTP handlers -> scratch SQL binding -> two authenticated provider listeners; %d provider calls; SQL readback %s unchanged after success/failure/denial", calls.Load(), before)
}
