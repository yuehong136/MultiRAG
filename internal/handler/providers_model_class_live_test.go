package handler_test

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/handler"
	"multirag/internal/logger"
	"multirag/internal/server"
	"multirag/internal/service"

	"github.com/gin-gonic/gin"
)

// Opt-in acceptance uses an owned scratch DB, fixture identity and real HTTP listeners.
func TestModelClassProviderLiveHTTPAndSQL(t *testing.T) {
	configPath := os.Getenv("MULTIRAG_MODEL_CLASS_LIVE_CONFIG")
	if configPath == "" {
		t.Skip("requires owned model-class scratch database config")
	}
	t.Chdir(filepath.Join("..", ".."))
	if err := logger.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(configPath); err != nil {
		t.Fatal("read owned config failed")
	}
	if !strings.HasPrefix(server.GetConfig().Database.Database, "multirag_model_class_") {
		t.Fatal("requires an owned model-class scratch DB")
	}
	if err := dao.InitDB(); err != nil {
		t.Fatal("initialize scratch DB failed")
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
		t.Fatal("open independent SQL readback failed")
	}
	defer readback.Close()
	active := "1"
	if err := dao.DB.Create(&entity.UserTenant{ID: "class-relation", UserID: "class-user", TenantID: "class-tenant", Role: "owner", InvitedBy: "class-user", Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	var requests atomic.Int32
	providerServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.Method != http.MethodPost || r.URL.Path != "/chat/completions" {
			t.Errorf("provider request = %s %s, incorrect endpoint or credential", r.Method, r.URL.Path)
		}
		var body struct{ Model string }
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body.Model != "qwen3-8b" && body.Model != "qwen/qwen3-8b" && body.Model != "glm-4.7-flash" {
			t.Errorf("provider model ID changed: %q", body.Model)
		}
		provider := "Gitee"
		if body.Model == "qwen/qwen3-8b" {
			provider = "SiliconFlow"
		}
		if r.Header.Get("Authorization") != "Bearer class-fixture-key-"+provider {
			t.Error("incorrect provider credential")
		}
		fmt.Fprint(w, `{"choices":[{"message":{"content":"<think>reason</think>answer"}}]}`)
	}))
	defer providerServer.Close()
	manager := dao.GetModelProviderManager()
	for _, name := range []string{"Gitee", "SiliconFlow"} {
		provider := manager.FindProvider(name)
		if provider == nil {
			t.Fatalf("%s config not loaded", name)
		}
		oldDriver, oldURL := provider.ModelDriver, provider.URL["default"]
		provider.URL["default"] = providerServer.URL
		provider.ModelDriver, err = models.NewModelFactory().CreateModelDriver(name, provider.URL, provider.URLSuffix)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			provider.ModelDriver, provider.URL["default"] = oldDriver, oldURL
		})
		if err := dao.DB.Create(&entity.TenantModelProvider{ID: name, TenantID: "class-tenant", ProviderName: name}).Error; err != nil {
			t.Fatal(err)
		}
		if err := dao.DB.Create(&entity.TenantModelInstance{ID: name + "-instance", ProviderID: name, InstanceName: "default", APIKey: "class-fixture-key-" + name, Extra: `{}`}).Error; err != nil {
			t.Fatal(err)
		}
	}
	svc := service.NewModelProviderService()
	h := handler.NewProviderHandler(nil, svc)
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	router.Use(func(c *gin.Context) {
		if c.GetHeader("Authorization") != "Bearer class-fixture-identity" {
			c.AbortWithStatus(http.StatusUnauthorized)
			return
		}
		c.Set("user", &entity.User{ID: "class-user"})
		c.Next()
	})
	router.POST("/api/v1/chat/completions", h.ChatToModel)
	apiServer := httptest.NewServer(router)
	defer apiServer.Close()
	for _, test := range []struct{ provider, model, class, answer, reasoning string }{
		{"Gitee", "qwen3-8b", "qwen3", "answer", "reason"},
		{"SiliconFlow", "qwen/qwen3-8b", "qwen3", "answer", "reason"},
		{"Gitee", "glm-4.7-flash", "glm", "<think>reason</think>answer", ""},
	} {
		t.Run(test.provider+"/"+test.model, func(t *testing.T) {
			data, err := json.Marshal(map[string]interface{}{"provider_name": test.provider, "instance_name": "default", "model_name": test.model, "message": "question", "model_class": "gpt", "model_type": "embedding"})
			if err != nil {
				t.Fatal(err)
			}
			req, err := http.NewRequest(http.MethodPost, apiServer.URL+"/api/v1/chat/completions", bytes.NewReader(data))
			if err != nil {
				t.Fatal(err)
			}
			req.Header.Set("Authorization", "Bearer class-fixture-identity")
			req.Header.Set("Content-Type", "application/json")
			response, err := apiServer.Client().Do(req)
			if err != nil {
				t.Fatal(err)
			}
			defer response.Body.Close()
			var payload struct {
				Code      int     `json:"code"`
				Answer    *string `json:"answer"`
				Reasoning *string `json:"reasoning_content"`
			}
			if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
				t.Fatal(err)
			}
			if response.StatusCode != http.StatusOK || payload.Code != 0 || payload.Answer == nil || *payload.Answer != test.answer {
				t.Fatalf("HTTP/semantic response = %d, %#v", response.StatusCode, payload)
			}
			if test.reasoning != "" && (payload.Reasoning == nil || *payload.Reasoning != test.reasoning) || test.reasoning == "" && payload.Reasoning != nil {
				t.Fatalf("reasoning response = %#v", payload)
			}
			compositeName := test.model + "@default@" + test.provider
			bound, err := svc.GetChatModel("class-tenant", compositeName)
			if err != nil || bound.ModelConfig.ModelClass == nil || *bound.ModelConfig.ModelClass != test.class {
				t.Fatalf("bound chat family = %#v, %v", bound, err)
			}
			if _, err := svc.GetEmbeddingModel("class-tenant", compositeName); err == nil {
				t.Fatal("family confused with embedding capability")
			}
		})
	}
	if requests.Load() != 3 {
		t.Fatalf("provider requests = %d, want 3", requests.Load())
	}
	var count int
	if err := readback.QueryRow("SELECT count(*) FROM tenant_model_instance WHERE api_key IN ($1, $2) AND extra = $3 AND status = $4", "class-fixture-key-Gitee", "class-fixture-key-SiliconFlow", "{}", "active").Scan(&count); err != nil || count != 2 {
		t.Fatalf("independent instance readback = %d, %v", count, err)
	}
}
