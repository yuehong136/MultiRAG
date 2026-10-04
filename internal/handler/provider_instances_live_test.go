package handler_test

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/handler"
	"multirag/internal/logger"
	"multirag/internal/server"
	"multirag/internal/service"
)

// Opt-in uses a private PostgreSQL database, controlled identities and real HTTP listeners.
func TestProviderInstancesLiveHTTPAndSQL(t *testing.T) {
	configPath := os.Getenv("MULTIRAG_PROVIDER_INSTANCES_CONFIG")
	if configPath == "" {
		t.Skip("requires owned provider-instance scratch config")
	}
	t.Chdir(filepath.Join("..", ".."))
	if err := logger.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(configPath); err != nil {
		t.Fatal("read owned config failed")
	}
	if !strings.HasPrefix(server.GetConfig().Database.Database, "multirag_provider_instances_") {
		t.Fatal("requires owned scratch DB")
	}
	if err := dao.InitDB(); err != nil {
		t.Fatal("initialize owned DB failed")
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
		t.Fatal("open readback failed")
	}
	defer readback.Close()
	active := "1"
	for _, user := range []string{"owner", "other"} {
		if err := dao.DB.Create(&entity.UserTenant{ID: user, UserID: user, TenantID: user + "-tenant", Role: "owner", InvitedBy: user, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	var calls [2]atomic.Int32
	var received [2]atomic.Value
	fixtures := make([]*httptest.Server, 2)
	for i := range fixtures {
		fixtures[i] = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls[i].Add(1)
			if r.URL.Path == "/v1/models" {
				if r.Method != "GET" || r.ContentLength > 0 {
					t.Error("invalid discovery request")
				}
				fmt.Fprint(w, `{"data":[{"id":"Qwen/custom"}]}`)
				return
			}
			if r.URL.Path != "/v1/chat/completions" {
				t.Errorf("bad path %s", r.URL.Path)
				w.WriteHeader(404)
				return
			}
			if r.Header.Get("Authorization") != fmt.Sprintf("Bearer fixture-key-%d", i) {
				t.Error("credential cross-contamination")
			}
			var body map[string]interface{}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Error(err)
			}
			received[i].Store(body["messages"])
			if body["model"] != "Qwen/custom" {
				t.Errorf("model %v", body)
			}
			if body["chat_template_kwargs"].(map[string]interface{})["enable_thinking"] != false {
				t.Errorf("explicit false lost: %v", body)
			}
			if body["stream"] == true {
				fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"reason-%d\",\"content\":\"answer-%d\"}}]}\n\ndata: [DONE]\n\n", i, i)
			} else {
				fmt.Fprintf(w, `{"choices":[{"message":{"content":"answer-%d","reasoning_content":"reason-%d"}}]}`, i, i)
			}
		}))
		defer fixtures[i].Close()
	}
	svc := service.NewModelProviderService()
	if code, err := svc.AddModelProvider("vllm", "owner"); code != common.CodeSuccess || err != nil {
		t.Fatalf("add provider %d %v", code, err)
	}
	h := handler.NewProviderHandler(nil, svc)
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	router.Use(func(c *gin.Context) {
		user := c.GetHeader("X-Fixture-User")
		if user != "" {
			c.Set("user", &entity.User{ID: user})
			c.Set("user_id", user)
		}
		c.Next()
	})
	router.POST("/api/v1/providers/:provider_name/instances", h.CreateProviderInstance)
	router.POST("/api/v1/providers/:provider_name/instances/:instance_name/models", h.AddCustomModel)
	router.PATCH("/api/v1/providers/:provider_name/instances/:instance_name/models/*model_name", h.EnableOrDisableModel)
	router.GET("/api/v1/providers/:provider_name/instances/:instance_name/models", h.ListInstanceModels)
	router.POST("/api/v1/chat/completions", h.ChatToModel)
	router.DELETE("/api/v1/providers/:provider_name/instances/:instance_name/models", h.DropInstanceModels)
	router.DELETE("/api/v1/providers/:provider_name/instances", h.DropProviderInstance)
	api := httptest.NewServer(router)
	defer api.Close()
	request := func(method, path, user string, body map[string]interface{}) (int, map[string]interface{}) {
		t.Helper()
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		req, err := http.NewRequest(method, api.URL+path, bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Fixture-User", user)
		response, err := api.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		var result map[string]interface{}
		if err := json.NewDecoder(response.Body).Decode(&result); err != nil {
			t.Fatal(err)
		}
		return response.StatusCode, result
	}
	for i := range fixtures {
		path := "/api/v1/providers/vllm/instances"
		instance := fmt.Sprintf("local-%d", i)
		status, result := request("POST", path, "owner", map[string]interface{}{"instance_name": instance, "api_key": fmt.Sprintf("fixture-key-%d", i), "base_url": fixtures[i].URL + "/v1", "region": "private"})
		if status != 200 || result["code"] != float64(0) {
			t.Fatalf("create instance %d %v", status, result)
		}
		_, duplicateInstance := request("POST", path, "owner", map[string]interface{}{"instance_name": instance, "api_key": fmt.Sprintf("different-key-%d", i), "base_url": fixtures[i].URL + "/v1"})
		if duplicateInstance["code"] != float64(common.CodeConflict) {
			t.Fatalf("ambiguous duplicate instance: %v", duplicateInstance)
		}
		path += "/" + instance + "/models"
		beforeCalls := calls[i].Load()
		_, result = request("POST", path, "owner", map[string]interface{}{"model_name": "Qwen/custom", "message": "legacy chat payload"})
		if result["code"] != float64(common.CodeBadRequest) || calls[i].Load() != beforeCalls {
			t.Fatalf("legacy chat payload reached provider or was accepted: %v", result)
		}
		var declarationCount int
		if err := readback.QueryRow(`SELECT count(*) FROM tenant_model m JOIN tenant_model_instance i ON i.id=m.instance_id WHERE m.model_name=$1 AND i.instance_name=$2`, "Qwen/custom", instance).Scan(&declarationCount); err != nil || declarationCount != 0 {
			t.Fatalf("legacy chat payload wrote a model declaration: count=%d, err=%v", declarationCount, err)
		}
		_, result = request("POST", path, "owner", map[string]interface{}{"model_name": "Qwen/custom", "model_type": "chat", "max_tokens": 131072, "thinking": true})
		if result["code"] != float64(0) {
			t.Fatalf("add model %v", result)
		}
		_, result = request("POST", path, "owner", map[string]interface{}{"model_name": "Qwen/custom", "model_type": "chat", "max_tokens": 131072})
		if result["code"] != float64(common.CodeConflict) {
			t.Fatalf("duplicate %v", result)
		}
		_, result = request("POST", path, "other", map[string]interface{}{"model_name": "foreign", "model_type": "chat", "max_tokens": 1024})
		if result["code"] == float64(0) {
			t.Fatal("cross-tenant model creation allowed")
		}
		_, result = request("POST", path, "owner", map[string]interface{}{"provider_name": "Gitee", "model_name": "mismatch", "model_type": "chat", "max_tokens": 1024})
		if result["code"] != float64(common.CodeBadRequest) {
			t.Fatalf("route mismatch %v", result)
		}
		_, result = request("GET", path, "owner", nil)
		rows := result["data"].([]interface{})
		if len(rows) != 1 {
			t.Fatalf("list %v", result)
		}
		row := rows[0].(map[string]interface{})
		if row["status"] != "active" || row["max_tokens"] != float64(131072) || row["thinking"] != true {
			t.Fatalf("model metadata %v", row)
		}
		names, err := svc.ListSupportedModels("vllm", instance, "owner")
		if err != nil || len(names) != 1 || names[0] != "Qwen/custom" {
			t.Fatalf("discovery %v %v", names, err)
		}
		if _, err := svc.CheckProviderConnection("vllm", instance, "owner"); err != nil {
			t.Fatal(err)
		}
		body := map[string]interface{}{"provider_name": "vllm", "instance_name": instance, "model_name": "Qwen/custom", "message": "q", "thinking": false}
		_, result = request("POST", "/api/v1/chat/completions", "owner", body)
		if result["code"] != float64(0) || result["answer"] != fmt.Sprintf("answer-%d", i) {
			t.Fatalf("chat %v", result)
		}

		multimodal := []map[string]interface{}{{"role": "system", "content": "rules"}, {"role": "assistant", "content": "previous", "reasoning_content": "thought"}, {"role": "user", "content": []interface{}{map[string]interface{}{"type": "text", "text": "describe"}, map[string]interface{}{"type": "image_url", "image_url": map[string]interface{}{"url": "https://example.com/a.png"}}}}}
		delete(body, "message")
		body["messages"] = multimodal
		_, result = request("POST", "/api/v1/chat/completions", "owner", body)
		encoded, _ := json.Marshal(multimodal)
		var expected interface{}
		json.Unmarshal(encoded, &expected)
		if result["code"] != float64(0) || result["answer"] != fmt.Sprintf("answer-%d", i) || !reflect.DeepEqual(received[i].Load(), expected) {
			t.Fatalf("multimodal history/response lost: %v %v", result, received[i].Load())
		}
		body["stream"] = true
		before := calls[i].Load()
		httpStatus, rejected := request("POST", "/api/v1/chat/completions", "owner", body)
		if httpStatus != http.StatusBadRequest || rejected["code"] != float64(common.CodeBadRequest) || calls[i].Load() != before {
			t.Fatalf("multimodal stream reached provider: %d %v", httpStatus, rejected)
		}
		textHistory := []map[string]interface{}{{"role": "system", "content": "rules"}, {"role": "assistant", "content": "previous"}, {"role": "user", "content": "next"}}
		body["messages"] = textHistory
		raw, _ := json.Marshal(body)
		streamRequest, _ := http.NewRequest("POST", api.URL+"/api/v1/chat/completions", bytes.NewReader(raw))
		streamRequest.Header.Set("Content-Type", "application/json")
		streamRequest.Header.Set("X-Fixture-User", "owner")
		streamResponse, err := http.DefaultClient.Do(streamRequest)
		if err != nil {
			t.Fatal(err)
		}
		streamBody, err := io.ReadAll(streamResponse.Body)
		streamResponse.Body.Close()
		if err != nil {
			t.Fatal(err)
		}
		encoded, _ = json.Marshal(textHistory)
		json.Unmarshal(encoded, &expected)
		if streamResponse.StatusCode != 200 || !strings.Contains(string(streamBody), "[DONE]") || !strings.Contains(string(streamBody), fmt.Sprintf("answer-%d", i)) || !reflect.DeepEqual(received[i].Load(), expected) {
			t.Fatalf("stream history lost: %s %v", streamBody, received[i].Load())
		}
		body["stream"] = false
		_, result = request("POST", "/api/v1/chat/completions", "other", body)
		if result["code"] == float64(0) {
			t.Fatal("cross-tenant chat allowed")
		}
		bound, err := svc.GetChatModel("owner-tenant", "Qwen/custom@"+instance+"@vllm")
		if err != nil {
			t.Fatal(err)
		}
		if bound.ModelConfig.Thinking == nil || !*bound.ModelConfig.Thinking {
			t.Fatal("stored thinking default lost")
		}
		if _, err := svc.GetEmbeddingModel("owner-tenant", "Qwen/custom@"+instance+"@vllm"); err == nil {
			t.Fatal("capability mismatch allowed")
		}
		answer, err := bound.Chat("system", []map[string]string{{"role": "assistant", "content": "history"}, {"role": "user", "content": "q"}}, map[string]interface{}{"thinking": false, "max_tokens": 64, "stop": []string{"STOP"}})
		if err != nil || answer != fmt.Sprintf("answer-%d", i) {
			t.Fatalf("bound %q %v", answer, err)
		}
		var frames []string
		err = bound.ChatStreamlyWithSender("system", []map[string]string{{"role": "user", "content": "q"}}, map[string]interface{}{"thinking": false}, func(content, reason *string) error {
			if content != nil {
				frames = append(frames, *content)
			}
			if reason != nil {
				frames = append(frames, *reason)
			}
			return nil
		})
		if err != nil || len(frames) != 3 || frames[2] != "[DONE]" {
			t.Fatalf("stream %v %v", frames, err)
		}
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		bound.APIConfig.Context = ctx
		_, err = bound.Chat("", []map[string]string{{"role": "user", "content": "q"}}, map[string]interface{}{"thinking": false})
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("cancel %v", err)
		}
		_, result = request("PATCH", path+"/Qwen/custom", "owner", map[string]interface{}{"status": "disable"})
		if result["code"] != float64(0) {
			t.Fatalf("disable %v", result)
		}
		if _, err := svc.GetChatModel("owner-tenant", "Qwen/custom@"+instance+"@vllm"); err == nil {
			t.Fatal("disabled custom model resolved")
		}
		_, result = request("GET", path, "owner", nil)
		if result["data"].([]interface{})[0].(map[string]interface{})["status"] != "inactive" {
			t.Fatal("disabled list status incorrect")
		}
		_, result = request("PATCH", path+"/Qwen/custom", "owner", map[string]interface{}{"status": "enable"})
		if result["code"] != float64(0) {
			t.Fatalf("enable %v", result)
		}
		var extra, statusDB, instanceExtra string
		if err := readback.QueryRow(`SELECT m.extra,m.status,i.extra FROM tenant_model m JOIN tenant_model_instance i ON i.id=m.instance_id WHERE m.model_name=$1 AND i.instance_name=$2`, "Qwen/custom", instance).Scan(&extra, &statusDB, &instanceExtra); err != nil {
			t.Fatal(err)
		}
		var fields map[string]interface{}
		json.Unmarshal([]byte(extra), &fields)
		if fields["max_tokens"] != float64(131072) || fields["thinking"] != true || statusDB != "active" || !strings.Contains(instanceExtra, fixtures[i].URL) {
			t.Fatalf("SQL readback %s %s %s", extra, statusDB, instanceExtra)
		}
	}
	// Concurrent duplicate declarations have one physical winner in this instance.
	var outcomes [6]common.ErrorCode
	var group sync.WaitGroup
	for i := range outcomes {
		group.Add(1)
		go func() {
			defer group.Done()
			outcomes[i], _ = svc.AddCustomModel(&service.AddCustomModelRequest{ProviderName: "vllm", InstanceName: "local-0", ModelName: "concurrent", ModelType: "chat", MaxTokens: 1024}, "owner")
		}()
	}
	group.Wait()
	winners := 0
	for _, code := range outcomes {
		if code == common.CodeSuccess {
			winners++
		} else if code != common.CodeConflict {
			t.Fatalf("concurrent add code %d", code)
		}
	}
	var physical int
	if err := readback.QueryRow("SELECT count(*) FROM tenant_model WHERE model_name='concurrent'").Scan(&physical); err != nil || physical != 1 || winners != 1 {
		t.Fatalf("duplicate winners %d rows %d %v", winners, physical, err)
	}
	if err := dao.DB.Create(&entity.Tenant{ID: "owner-tenant", Status: &active, LLMID: "Qwen/custom@local-0@vllm"}).Error; err != nil {
		t.Fatal(err)
	}
	defaultModel, err := svc.GetChatModel("owner-tenant", "")
	if err != nil || defaultModel.ModelConfig.Thinking == nil || !*defaultModel.ModelConfig.Thinking {
		t.Fatalf("default custom model %v %v", defaultModel, err)
	}
	instances, code, err := svc.ListProviderInstances("vllm", "owner")
	if code != common.CodeSuccess || err != nil || len(instances) != 2 || instances[0]["extra"] == nil || instances[0]["region"] != "private" {
		t.Fatalf("instance extras %v %v", instances, err)
	}
	status, _ := request("POST", "/api/v1/providers/vllm/instances/local-0/models", "", map[string]interface{}{"model_name": "noauth", "model_type": "chat", "max_tokens": 1})
	if status != http.StatusUnauthorized {
		t.Fatal("unauthenticated creation allowed")
	}
	if dao.GetModelProviderManager().FindProvider("vllm").URL["default"] != "" {
		t.Fatal("global provider URLs polluted")
	}
	if calls[0].Load() != 7 || calls[1].Load() != 7 {
		t.Fatalf("unexpected provider calls %d %d", calls[0].Load(), calls[1].Load())
	}
	if err := dao.DB.Model(&entity.TenantModel{}).Where("model_name = ?", "Qwen/custom").Update("extra", "invalid").Error; err != nil {
		t.Fatal(err)
	}
	if _, err := svc.GetChatModel("owner-tenant", "Qwen/custom@local-0@vllm"); err == nil {
		t.Fatal("corrupt model extra accepted")
	}
	if err := dao.DB.Model(&entity.TenantModel{}).Where("model_name = ?", "Qwen/custom").Update("extra", `{"max_tokens":131072,"thinking":true}`).Error; err != nil {
		t.Fatal(err)
	}
	exerciseModelDeletion(t, svc, readback, request)
	exerciseCatalogDeletionRace(t, svc, readback)
	if err := pool.Close(); err != nil {
		t.Fatal(err)
	}
	if _, _, err := svc.ChatToModel("vllm", "local-0", "Qwen/custom", "owner", "q", &models.APIConfig{}, &models.ChatConfig{}); err == nil {
		t.Fatal("database failure became success")
	}
}
