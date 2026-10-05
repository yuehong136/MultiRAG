package handler_test

import (
	"encoding/json"
	"fmt"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/logger"
	"multirag/internal/server"
	"multirag/internal/service"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestOllamaInstanceLiveHTTPAndSQL(t *testing.T) {
	config := os.Getenv("MULTIRAG_PROVIDER_INSTANCES_CONFIG")
	if config == "" {
		t.Skip("requires owned provider scratch database")
	}
	t.Chdir(filepath.Join("..", ".."))
	if err := logger.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal("load scratch config failed")
	}
	if !strings.HasPrefix(server.GetConfig().Database.Database, "multirag_provider_instances_") {
		t.Fatal("requires owned scratch")
	}
	if err := dao.InitDB(); err != nil {
		t.Fatal("initialize scratch failed")
	}
	pool, err := dao.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	active := "1"
	if err := dao.DB.Create(&entity.UserTenant{ID: "ollama-owner", UserID: "ollama-owner", TenantID: "ollama-tenant", Role: "owner", InvitedBy: "ollama-owner", Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	svc := service.NewModelProviderService()
	if code, err := svc.AddModelProvider("ollama", "ollama-owner"); err != nil || code != common.CodeSuccess {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/api/tags" {
				fmt.Fprint(w, `{"models":[{"name":"qwen3"}]}`)
				return
			}
			if r.URL.Path != "/api/chat" {
				t.Error(r.URL.Path)
			}
			var body struct{ Stream bool }
			json.NewDecoder(r.Body).Decode(&body)
			fmt.Fprintf(w, `{"message":{"content":"answer-%d","thinking":"reason"},"done":true}`, i)
		}))
		defer fixture.Close()
		instance := fmt.Sprintf("local-%d", i)
		key := ""
		if i == 1 {
			key = "optional-key"
		}
		if code, err := svc.CreateProviderInstance("ollama", instance, key, "ollama-owner", "", fixture.URL); err != nil || code != common.CodeSuccess {
			t.Fatal(err)
		}
		if code, err := svc.AddCustomModel(&service.AddCustomModelRequest{ProviderName: "ollama", InstanceName: instance, ModelName: "qwen3", ModelType: "chat", MaxTokens: 2048}, "ollama-owner"); err != nil || code != common.CodeSuccess {
			t.Fatal(err)
		}
		if names, err := svc.ListSupportedModels("ollama", instance, "ollama-owner"); err != nil || !reflect.DeepEqual(names, []string{"qwen3"}) {
			t.Fatal(names, err)
		}
		if _, err := svc.CheckProviderConnection("ollama", instance, "ollama-owner"); err != nil {
			t.Fatal(err)
		}
		response, code, err := svc.ChatToModel("ollama", instance, "qwen3", "ollama-owner", "question", nil, nil)
		if err != nil || code != common.CodeSuccess || *response.Answer != fmt.Sprintf("answer-%d", i) {
			t.Fatal(response, err)
		}
		var frames []string
		code, err = svc.ChatToModelStreamWithMessages("ollama", instance, "qwen3", "ollama-owner", []models.Message{{Role: "user", Content: "q"}}, nil, nil, func(c, r *string) error {
			if c != nil {
				frames = append(frames, *c)
			}
			return nil
		})
		if err != nil || code != common.CodeSuccess || !reflect.DeepEqual(frames, []string{fmt.Sprintf("answer-%d", i), "[DONE]"}) {
			t.Fatal(frames, err)
		}
	}
	if dao.GetModelProviderManager().FindProvider("ollama").URL["default"] != "" {
		t.Fatal("shared URL changed")
	}
	var count int
	if err := pool.QueryRow(`SELECT count(*) FROM tenant_model_instance WHERE provider_id IN (SELECT id FROM tenant_model_provider WHERE tenant_id='ollama-tenant')`).Scan(&count); err != nil || count != 2 {
		t.Fatal("instance readback failed")
	}
}
