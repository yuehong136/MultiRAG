package router

import (
	"database/sql"
	"encoding/json"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/server/local"
	"multirag/internal/service"
)

func TestMemoryUpdateHTTPAndSQL(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_AFTER9D_B_MEMORY_DSN")
	if dsn == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned memory scratch database required")
		}
		t.Skip("requires owned memory scratch database")
	}
	address, err := url.Parse(dsn)
	if err != nil || !strings.HasPrefix(strings.TrimPrefix(address.Path, "/"), "multirag_after9d_b_memory_") {
		t.Fatal("unowned database")
	}
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal(err)
	}
	pool, _ := db.DB()
	t.Cleanup(func() { pool.Close() })
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { readback.Close() })
	prev := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = prev })
	previousAdmin := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned memory test")
	t.Cleanup(func() { local.SetAdminStatus(previousAdmin.Status, previousAdmin.Reason) })
	if err := db.AutoMigrate(&entity.User{}, &entity.APIToken{}, &entity.Memory{}); err != nil {
		t.Fatal(err)
	}
	active, access := "1", "active-memory-session"
	for _, id := range []string{"memory-owner", "memory-foreign"} {
		if err := db.Create(&entity.User{ID: id, Email: id + "@example.invalid", Nickname: id, AccessToken: &access, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Create(&entity.APIToken{TenantID: id, Token: "memory-token-" + id}).Error; err != nil {
			t.Fatal(err)
		}
	}
	zero, embd := int64(0), int64(20)
	description := "before"
	prompt := service.PromptAssembler{}.AssembleSystemPrompt([]string{"raw"})
	for _, m := range []*entity.Memory{{ID: "filled", Name: "memory", TenantID: "memory-owner", MemoryType: dao.CalculateMemoryType([]string{"raw", "semantic"}), MemorySize: 100, EmbdID: "embd", LLMID: "llm", TenantEmbdID: &embd, TenantLLMID: &zero, Permissions: "me", ForgettingPolicy: "FIFO", Temperature: 0.5, Description: &description, SystemPrompt: &prompt}, {ID: "empty", Name: "empty", TenantID: "memory-owner", MemorySize: 1, MemoryType: dao.CalculateMemoryType([]string{"raw"}), EmbdID: "embd", LLMID: "llm", Permissions: "me", ForgettingPolicy: "FIFO", SystemPrompt: &prompt}} {
		if err := db.Create(m).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := db.Model(&entity.Memory{}).Where("id = ?", "empty").Update("memory_size", 0).Error; err != nil {
		t.Fatal(err)
	}
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	(&Router{authHandler: handler.NewAuthHandler(), memoryHandler: handler.NewMemoryHandler(service.NewMemoryService())}).Setup(app)
	request := func(id, body, actor string) map[string]any {
		t.Helper()
		w := httptest.NewRecorder()
		req := httptest.NewRequest("PUT", "/api/v1/memories/"+id, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		if actor != "" {
			req.Header.Set("Authorization", "Bearer memory-token-"+actor)
		}
		app.ServeHTTP(w, req)
		var result map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil {
			t.Fatal(err)
		}
		return result
	}
	snapshot := func() string {
		t.Helper()
		var raw string
		if err := readback.QueryRow(`SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM t_ai_memories x`).Scan(&raw); err != nil {
			t.Fatal(err)
		}
		return raw
	}
	before := snapshot()
	for _, body := range []string{`{}`, `{"name":"memory"}`, `{"name":" memory ","permissions":" ME ","forgetting_policy":" fifo ","llm_id":"llm","embd_id":"embd","tenant_llm_id":"0","tenant_embd_id":"20","memory_type":["semantic","RAW","semantic"],"temperature":0.5,"memory_size":100,"description":"before"}`} {
		out := request("filled", body, "memory-owner")
		if out["code"] != float64(0) {
			t.Fatal("same config failed", out)
		}
		if snapshot() != before {
			t.Fatal("same config changed SQL/timestamps or prompts", body)
		}
	}
	for _, body := range []string{`{"memory_type":["episodic"]}`, `{"embd_id":"different"}`, `{"tenant_embd_id":"21"}`, `{"forgetting_policy":"invalid"}`, `{"tenant_llm_id":"invalid"}`, `{"memory_type":[]}`} {
		out := request("filled", body, "memory-owner")
		if out["code"] == float64(0) {
			t.Fatal("invalid or protected update accepted", body)
		}
		if snapshot() != before {
			t.Fatal("rejected update changed memory")
		}
	}
	for _, actor := range []string{"", "memory-foreign"} {
		out := request("filled", `{"description":"forbidden"}`, actor)
		if out["code"] == float64(0) {
			t.Fatal("foreign update accepted")
		}
		if snapshot() != before {
			t.Fatal("foreign request changed memory")
		}
	}
	out := request("filled", `{"temperature":0,"description":"","avatar":"","permissions":"team"}`, "memory-owner")
	if out["code"] != float64(0) {
		t.Fatal(out)
	}
	var temp float64
	var desc, avatar, permissions string
	if err := readback.QueryRow(`SELECT temperature,description,avatar,permissions FROM t_ai_memories WHERE id='filled'`).Scan(&temp, &desc, &avatar, &permissions); err != nil || temp != 0 || desc != "" || avatar != "" || permissions != "team" {
		t.Fatal("explicit zero/empty/permission update missing", err)
	}
	before = snapshot()
	out = request("filled", `{"temperature":0,"description":"","avatar":"","permissions":"team"}`, "memory-owner")
	if out["code"] != float64(0) || snapshot() != before {
		t.Fatal("second identical update was not a no-op")
	}
	out = request("empty", `{"memory_type":["semantic","semantic"],"tenant_embd_id":"0"}`, "memory-owner")
	if out["code"] != float64(0) {
		t.Fatal("empty memory configuration update failed", out)
	}
	var kind, tenantEmbd int64
	var updatedPrompt string
	if err := readback.QueryRow(`SELECT memory_type,tenant_embd_id,system_prompt FROM t_ai_memories WHERE id='empty'`).Scan(&kind, &tenantEmbd, &updatedPrompt); err != nil || kind != dao.CalculateMemoryType([]string{"semantic"}) || tenantEmbd != 0 || updatedPrompt != (service.PromptAssembler{}).AssembleSystemPrompt([]string{"semantic"}) {
		t.Fatal("type/prompt/explicit zero mismatch", err)
	}
	before = snapshot()
	out = request("empty", `{"memory_type":["semantic"],"tenant_embd_id":"0"}`, "memory-owner")
	if out["code"] != float64(0) || snapshot() != before {
		t.Fatal("same empty-memory config was not a no-op")
	}
}
