package admin

import (
	"multirag/internal/entity"
	"multirag/internal/server"
	"strings"
	"testing"
)

func TestAfterFFAdminRegistrationSQL(t *testing.T) {
	f := newAdminFixture(t)
	cfg := server.GetConfig()
	cfg.UserDefaultLLM.DefaultModels.ChatModel = server.ModelConfig{Factory: "OpenAI", Name: "fixture-chat", APIKey: "owned-secret", BaseURL: "https://fixture.invalid/v1"}
	if err := f.DB.Create(&entity.LLM{LLMName: "fixture-chat", FID: "OpenAI", ModelType: "chat", Tags: "LLM", MaxTokens: 128}).Error; err != nil {
		t.Fatal(err)
	}
	status, result := f.request(t, "admin", "POST", "/users", `{"username":"registered@fixture.invalid","password":"owned-password!","role":"user"}`)
	if status != 200 || result["code"] != float64(0) {
		t.Fatal(status, result)
	}
	var id, hash, tenantName, chat string
	if err := f.Read.QueryRow("SELECT id,password FROM user WHERE email=?", "registered@fixture.invalid").Scan(&id, &hash); err != nil || !CheckWerkzeugPassword("owned-password!", hash) || strings.Contains(hash, "owned-password!") {
		t.Fatal("password not verified", err)
	}
	if err := f.Read.QueryRow("SELECT name,llm_id FROM tenant WHERE id=?", id).Scan(&tenantName, &chat); err != nil || chat != "fixture-chat" || tenantName != "'s Kingdom" {
		t.Fatal("target defaults missing", err)
	}
	var role, statusString string
	if err := f.Read.QueryRow("SELECT role,status FROM user_tenant WHERE user_id=? AND tenant_id=?", id, id).Scan(&role, &statusString); err != nil || role != "owner" || statusString != "1" {
		t.Fatal("owner membership missing", err)
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM file WHERE tenant_id=? AND type='folder' AND parent_id=id", id).Scan(&count); err != nil || count != 1 {
		t.Fatal("root folder absent", err)
	}
	if err := f.DB.Exec("CREATE TRIGGER reject_admin_model BEFORE INSERT ON tenant_llm FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='controlled registration model failure'").Error; err != nil {
		t.Fatal(err)
	}
	status, result = f.request(t, "admin", "POST", "/users", `{"username":"failed@fixture.invalid","password":"owned-password!","role":"user"}`)
	if status < 400 || result["code"] == float64(0) {
		t.Fatal("registration failure accepted", status, result)
	}
	if err := f.Read.QueryRow("SELECT count(*) FROM user WHERE email=?", "failed@fixture.invalid").Scan(&count); err != nil || count != 0 {
		t.Fatal("failed user persisted", err)
	}
	for _, table := range []string{"tenant", "user_tenant", "file", "tenant_llm"} {
		var n int
		if err := f.Read.QueryRow("SELECT count(*) FROM "+table+" WHERE "+map[string]string{"tenant": "id", "user_tenant": "tenant_id", "file": "tenant_id", "tenant_llm": "tenant_id"}[table]+" NOT IN (?,?)", id, "admin").Scan(&n); err != nil || n != 0 {
			t.Fatal("failed registration side effects", table, n, err)
		}
	}
}
