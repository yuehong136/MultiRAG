package handler_test

import (
	"encoding/json"
	"fmt"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
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

func TestXAIInstanceLiveHTTPAndSQL(t *testing.T) {
	config := os.Getenv("MULTIRAG_PROVIDER_INSTANCES_CONFIG")
	if config == "" {
		t.Skip("requires owned provider scratch database")
	}
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
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
	if err := dao.DB.Create(&entity.UserTenant{ID: "xai-owner", UserID: "xai-owner", TenantID: "xai-tenant", Role: "owner", InvitedBy: "xai-owner", Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	svc := service.NewModelProviderService()
	if code, err := svc.AddModelProvider("xAI", "xai-owner"); err != nil || code != common.CodeSuccess {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/v1/models" {
				fmt.Fprint(w, `{"data":[{"id":"grok-test"}]}`)
				return
			}
			if r.URL.Path != "/v1/chat/completions" {
				t.Error(r.URL.Path)
			}
			var body struct{ Stream bool }
			json.NewDecoder(r.Body).Decode(&body)
			if body.Stream {
				fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer-%d\",\"reasoning_content\":\"reason\"},\"finish_reason\":\"stop\"}]}\n\n", i)
			} else {
				fmt.Fprintf(w, `{"choices":[{"message":{"content":"answer-%d","reasoning_content":"reason"}}]}`, i)
			}
		}))
		defer fixture.Close()
		instance := fmt.Sprintf("local-%d", i)
		key := "xai-key-one"
		if i == 1 {
			key = "xai-key-two"
		}
		if code, err := svc.CreateProviderInstance("xAI", instance, key, "xai-owner", "", fixture.URL+"/v1"); err != nil || code != common.CodeSuccess {
			t.Fatal(err)
		}
		if code, err := svc.AddCustomModel(&service.AddCustomModelRequest{ProviderName: "xAI", InstanceName: instance, ModelName: "grok-test", ModelType: "chat", MaxTokens: 2048}, "xai-owner"); err != nil || code != common.CodeSuccess {
			t.Fatal(err)
		}
		if names, err := svc.ListSupportedModels("xAI", instance, "xai-owner"); err != nil || !reflect.DeepEqual(names, []string{"grok-test"}) {
			t.Fatal(names, err)
		}
		if _, err := svc.CheckProviderConnection("xAI", instance, "xai-owner"); err != nil {
			t.Fatal(err)
		}
		response, code, err := svc.ChatToModel("xAI", instance, "grok-test", "xai-owner", "question", nil, nil)
		if err != nil || code != common.CodeSuccess || *response.Answer != fmt.Sprintf("answer-%d", i) {
			t.Fatal(response, err)
		}
		var frames []string
		code, err = svc.ChatToModelStreamWithMessages("xAI", instance, "grok-test", "xai-owner", []models.Message{{Role: "user", Content: "q"}}, nil, nil, func(c, r *string) error {
			if c != nil {
				frames = append(frames, *c)
			}
			return nil
		})
		if err != nil || code != common.CodeSuccess || !reflect.DeepEqual(frames, []string{fmt.Sprintf("answer-%d", i), "[DONE]"}) {
			t.Fatal(frames, err)
		}
	}
	if dao.GetModelProviderManager().FindProvider("xAI").URL["default"] != "https://api.x.ai/v1" {
		t.Fatal("shared URL changed")
	}
	var count int
	if err := pool.QueryRow(`SELECT count(*) FROM tenant_model_instance WHERE provider_id IN (SELECT id FROM tenant_model_provider WHERE tenant_id='xai-tenant')`).Scan(&count); err != nil || count != 2 {
		t.Fatal("instance readback failed")
	}
}
