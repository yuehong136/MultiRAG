package handler_test

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/service"
)

// Controlled authentication and Gemini responses, actual SDK/TCP and scratch SQL.
func TestGoogleEmbeddingLiveHTTPAndSQL(t *testing.T) {
	configPath := os.Getenv("MULTIRAG_GOOGLE_EMBED_CONFIG")
	if configPath == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned Google embedding scratch config required")
		}
		t.Skip("requires owned Google embedding scratch config")
	}
	recordOwnedOptInTestBinary(t)
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(configPath); err != nil {
		t.Fatal("load scratch config failed")
	}
	if !strings.HasPrefix(server.GetConfig().Database.Database, "multirag_google_embed_") {
		t.Fatal("owned Google embedding scratch database required")
	}
	if err := dao.InitDB(); err != nil {
		t.Fatal("initialize scratch failed", err)
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
		t.Fatal("independent readback connection failed")
	}
	defer readback.Close()
	var version string
	if err := readback.QueryRow("SELECT version()").Scan(&version); err != nil {
		t.Fatal(err)
	}
	t.Log("scratch backend:", version)
	var calls atomic.Int32
	provider := newOwnedOptInHTTPServer(t, "google-provider", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		instance := strings.Split(strings.TrimPrefix(r.URL.Path, "/"), "/")[0]
		if (instance != "one" && instance != "two") || r.Header.Get("x-goog-api-key") != "controlled-"+instance {
			t.Error("instance credentials or base URL crossed")
		}
		if r.URL.Path != "/"+instance+"/v1beta/models/text-embedding-004:batchEmbedContents" {
			t.Error("SDK endpoint lost")
		}
		var body struct {
			Requests []struct {
				Content              struct{ Parts []struct{ Text string } }
				TaskType             string
				OutputDimensionality int
			}
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil || len(body.Requests) != 2 {
			t.Error("batch payload", err)
			w.WriteHeader(400)
			return
		}
		for _, request := range body.Requests {
			if request.TaskType != "RETRIEVAL_DOCUMENT" || request.OutputDimensionality != 2 {
				t.Error("document task or dimension lost")
			}
		}
		if body.Requests[0].Content.Parts[0].Text == "bad" {
			fmt.Fprint(w, `{"embeddings":[{"values":[1,null]},{"values":[2,3]}]}`)
			return
		}
		fmt.Fprint(w, `{"embeddings":[{"values":[0,1]},{"values":[2,0]}]}`)
	}))
	defer provider.Close()
	active := "1"
	for _, id := range []string{"owner", "other"} {
		if err := dao.DB.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: id + "-tenant", Role: "owner", InvitedBy: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := dao.DB.Create(&entity.TenantModelProvider{ID: "google", ProviderName: "Google", TenantID: "owner-tenant"}).Error; err != nil {
		t.Fatal(err)
	}
	for _, instance := range []string{"one", "two"} {
		extra, _ := json.Marshal(map[string]string{"region": "office", "base_url": provider.URL + "/" + instance})
		if err := dao.DB.Create(&entity.TenantModelInstance{ID: instance, ProviderID: "google", InstanceName: instance, APIKey: "controlled-" + instance, Status: "active", Extra: string(extra)}).Error; err != nil {
			t.Fatal(err)
		}
	}
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	router.Use(func(c *gin.Context) {
		user := strings.TrimPrefix(c.GetHeader("Authorization"), "Bearer ")
		if user != "owner" && user != "other" {
			c.AbortWithStatus(http.StatusUnauthorized)
			return
		}
		c.Set("user", &entity.User{ID: user})
		c.Set("user_id", user)
		c.Next()
	})
	h := handler.NewProviderHandler(nil, service.NewModelProviderService())
	router.POST("/api/v1/embeddings", h.EmbedText)
	app := newOwnedOptInHTTPServer(t, "google-api", router)
	defer app.Close()
	request := func(user, instance, model, first string, dimension int) (int, *int, []struct {
		Index     int
		Embedding []float64
	}) {
		t.Helper()
		body, _ := json.Marshal(map[string]any{"provider_name": "Google", "instance_name": instance, "model_name": model, "texts": []string{first, "second"}, "dimension": dimension})
		req, _ := http.NewRequest(http.MethodPost, app.URL+"/api/v1/embeddings", bytes.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+user)
		req.Header.Set("Content-Type", "application/json")
		response, err := app.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		data, err := io.ReadAll(response.Body)
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(data), "controlled-") {
			t.Fatal("provider credentials leaked")
		}
		var result struct {
			Code *int
			Data []struct {
				Index     int
				Embedding []float64
			}
		}
		if response.StatusCode != http.StatusUnauthorized {
			if err := json.Unmarshal(data, &result); err != nil || result.Code == nil {
				t.Fatal("missing business code", err)
			}
		}
		return response.StatusCode, result.Code, result.Data
	}
	for _, instance := range []string{"one", "two"} {
		status, code, data := request("owner", instance, "text-embedding-004", "first", 2)
		if status != http.StatusOK || code == nil || *code != 0 || len(data) != 2 || data[0].Index != 0 || data[1].Index != 1 || len(data[0].Embedding) != 2 || len(data[1].Embedding) != 2 {
			t.Fatalf("embedding response status %d code %v data %v", status, code, data)
		}
	}
	if calls.Load() != 2 {
		t.Fatal("batch did not use one request per instance")
	}
	_, code, _ := request("owner", "one", "text-embedding-004", "bad", 2)
	if code == nil || *code == 0 || calls.Load() != 3 {
		t.Fatal("malformed provider vector succeeded")
	}
	before := calls.Load()
	for _, tc := range []struct {
		user, model string
		dimension   int
	}{{"other", "text-embedding-004", 2}, {"owner", "gemini-2.5-flash", 2}, {"owner", "text-embedding-004", -1}} {
		_, code, _ := request(tc.user, "one", tc.model, "first", tc.dimension)
		if code == nil || *code == 0 || calls.Load() != before {
			t.Fatal("denied request reached provider or succeeded")
		}
	}
	if status, _, _ := request("anonymous", "one", "text-embedding-004", "first", 2); status != http.StatusUnauthorized || calls.Load() != before {
		t.Fatal("unauthenticated request reached provider")
	}
	var instanceCount, modelCount int
	if err := readback.QueryRow("SELECT count(*) FROM tenant_model_instance").Scan(&instanceCount); err != nil || instanceCount != 2 {
		t.Fatal("instance readback failed")
	}
	if err := readback.QueryRow("SELECT count(*) FROM tenant_model").Scan(&modelCount); err != nil || modelCount != 0 {
		t.Fatal("inference unexpectedly wrote model state")
	}
	for _, instance := range []string{"one", "two"} {
		var key, extra string
		if err := readback.QueryRow("SELECT api_key,extra FROM tenant_model_instance WHERE instance_name=$1", instance).Scan(&key, &extra); err != nil {
			t.Fatal("instance configuration readback failed")
		}
		var values map[string]string
		if err := json.Unmarshal([]byte(extra), &values); err != nil || key != "controlled-"+instance || !reflect.DeepEqual(values, map[string]string{"region": "office", "base_url": provider.URL + "/" + instance}) {
			t.Fatal("instance configuration changed")
		}
	}
	t.Logf("actual HTTP handler -> service/catalog -> isolated instance -> Gemini SDK: %d provider requests; independent SQL confirmed unchanged instances and no model writes", calls.Load())
}
