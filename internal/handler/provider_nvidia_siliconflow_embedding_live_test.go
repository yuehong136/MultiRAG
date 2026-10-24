package handler_test

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
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

func TestNVIDIASiliconFlowEmbeddingLiveCLIHTTPAndSQL(t *testing.T) {
	config := os.Getenv("MULTIRAG_H_EMBEDDING_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned embedding scratch config required")
		}
		t.Skip("requires owned embedding scratch database")
	}
	recordOwnedOptInTestBinary(t)
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal("load private config failed")
	}
	if !strings.HasPrefix(server.GetConfig().Database.Database, "multirag_inference_h_") {
		t.Fatal("requires owned embedding scratch database")
	}
	if err := dao.InitDB(); err != nil {
		t.Fatal("initialize owned scratch database failed", err)
	}
	pool, err := dao.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	dsn, err := dao.PostgresDSN(server.GetConfig().Database)
	if err != nil {
		t.Fatal(err)
	}
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer readback.Close()
	active := "1"
	for _, id := range []string{"owner", "other"} {
		if err := dao.DB.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: id + "-tenant", Role: "owner", InvitedBy: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	var calls atomic.Int32
	for _, providerName := range []string{"NVIDIA", "SiliconFlow"} {
		provider := &entity.TenantModelProvider{ID: providerName, ProviderName: providerName, TenantID: "owner-tenant"}
		if err := dao.DB.Create(provider).Error; err != nil {
			t.Fatal(err)
		}
		for _, instance := range []string{"one", "two"} {
			fixture := newOwnedOptInHTTPServer(t, strings.ToLower(providerName)+"-"+instance, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				var body struct {
					Input          []string
					Dimensions     int
					InputType      string `json:"input_type"`
					EncodingFormat string `json:"encoding_format"`
					Truncate       string
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if r.URL.Path != "/v1/embeddings" || r.Header.Get("Authorization") != "Bearer "+providerName+instance || body.Dimensions != 2 || len(body.Input) > 32 {
					t.Error("provider request lost configuration", body, r.URL.Path)
				}
				if providerName == "NVIDIA" && (body.InputType != "passage" || body.EncodingFormat != "float" || body.Truncate != "END") {
					t.Error("document embedding semantics lost", body)
				}
				if len(body.Input) > 0 && body.Input[0] == "bad" {
					w.WriteHeader(http.StatusServiceUnavailable)
					return
				}
				data := make([]map[string]any, 0, len(body.Input))
				for i := len(body.Input) - 1; i >= 0; i-- {
					data = append(data, map[string]any{"index": i, "embedding": []float64{0, 1}})
				}
				json.NewEncoder(w).Encode(map[string]any{"object": "list", "data": data})
			}))
			defer fixture.Close()
			extra, _ := json.Marshal(map[string]string{"region": "office", "base_url": fixture.URL + "/v1/"})
			row := &entity.TenantModelInstance{ID: providerName + instance, ProviderID: providerName, InstanceName: instance, APIKey: providerName + instance, Status: "active", Extra: string(extra)}
			if err := dao.DB.Create(row).Error; err != nil {
				t.Fatal(err)
			}
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
	api := newOwnedOptInHTTPServer(t, "embedding-api", router)
	defer api.Close()
	parsed, _ := url.Parse(api.URL)
	port, _ := strconv.Atoi(parsed.Port())
	snapshot := func() string {
		t.Helper()
		var digest string
		for _, table := range []string{"tenant_model_provider", "tenant_model_instance", "tenant_model", "t_ai_user_tenants"} {
			var value string
			// These are literal test-owned table names, never request input.
			if err := readback.QueryRow("SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY id)::text,'')) FROM " + table + " t").Scan(&value); err != nil {
				t.Fatal("independent SQL readback failed", err)
			}
			digest += value
		}
		return digest
	}
	before := snapshot()
	for _, provider := range []string{"NVIDIA", "SiliconFlow"} {
		name := "baai/bge-m3"
		if provider == "SiliconFlow" {
			name = "Qwen/Qwen3-Embedding-0.6B"
		}
		for _, instance := range []string{"one", "two"} {
			for _, mode := range []string{"success", "later failure", "other tenant", "unauthenticated"} {
				httpClient := cli.NewHTTPClient()
				httpClient.Host, httpClient.Port, httpClient.APIKey = parsed.Hostname(), port, "owner"
				if mode == "other tenant" {
					httpClient.APIKey = "other"
				}
				if mode == "unauthenticated" {
					// A nonempty unrecognized credential reaches the handler's identity boundary.
					httpClient.APIKey = "unknown"
				}
				client := cli.NewMultiRAGClient("user")
				client.HTTPClient = httpClient
				texts := strings.Repeat("'a' ", 33)
				if mode == "later failure" {
					texts = strings.Repeat("'a' ", 32) + "'bad' "
				}
				command, err := cli.NewParser(fmt.Sprintf("embed text %swith '%s@%s@%s' dimension 2;", texts, name, instance, provider)).Parse(false)
				if err != nil {
					t.Fatal(err)
				}
				n := calls.Load()
				result, err := client.ExecuteUserCommand(command)
				if mode == "success" {
					if err != nil || result == nil || calls.Load()-n != 2 {
						t.Fatal(provider, instance, mode, result, err, calls.Load()-n)
					}
					rows := result.(*cli.EmbeddingsResponse).Data
					if len(rows) != 33 || rows[32].Index != 32 || len(rows[32].Embedding) != 2 {
						t.Fatal(rows)
					}
				} else {
					wantCalls := int32(0)
					if mode == "later failure" {
						wantCalls = 2
					}
					if err == nil || result != nil || calls.Load()-n != wantCalls {
						t.Fatal("failure or denial returned success", provider, instance, mode, result, err, calls.Load()-n)
					}
				}
				if snapshot() != before {
					t.Fatal("inference changed SQL state", provider, instance, mode)
				}
			}
		}
	}
	t.Logf("real CLI -> handler -> owned PostgreSQL binding -> controlled provider HTTP; calls=%d; independent SQL contents unchanged after success, later-batch failure and denials", calls.Load())
}
