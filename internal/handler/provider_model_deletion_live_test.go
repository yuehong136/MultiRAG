package handler_test

import (
	"database/sql"
	"encoding/json"
	"net/http"
	"reflect"
	"strings"
	"testing"
	"time"

	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/service"
)

// Called by the owned PostgreSQL/HTTP fixture; all counts use a second connection.
func exerciseModelDeletion(t *testing.T, svc *service.ModelProviderService, db *sql.DB, request func(string, string, string, map[string]interface{}) (int, map[string]interface{})) {
	t.Helper()
	path := "/api/v1/providers/vllm/instances/local-0/models"
	check := func(method, route, user string, body map[string]interface{}, want common.ErrorCode) {
		t.Helper()
		status, result := request(method, route, user, body)
		if status != 200 || result["code"] != float64(want) {
			t.Fatalf("%s %s: %d %v want %d", method, route, status, result, want)
		}
		raw, _ := json.Marshal(result)
		if strings.Contains(string(raw), "secret-delete") {
			t.Fatal("SQL error leaked")
		}
	}
	count := func(table, tenant string) int {
		t.Helper()
		var value int
		if err := db.QueryRow("SELECT count(*) FROM "+table+" m JOIN tenant_model_provider p ON p.id=m.provider_id WHERE p.tenant_id=$1", tenant).Scan(&value); err != nil {
			t.Fatal(err)
		}
		return value
	}
	for _, body := range []map[string]interface{}{
		{"model_name": "invalid", "model_types": []string{}, "max_tokens": 10},
		{"model_name": "invalid", "model_types": []string{"chat", "chat"}, "max_tokens": 10},
		{"model_name": "invalid", "model_type": "embedding", "model_types": []string{"chat", "embedding"}, "max_tokens": 10},
	} {
		check("POST", path, "owner", body, common.CodeBadRequest)
	}
	check("POST", path, "owner", map[string]interface{}{"model_name": "multi", "model_types": []string{"chat", "vision", "embedding"}, "max_tokens": 1024, "thinking": false}, common.CodeSuccess)
	var primary, raw string
	if err := db.QueryRow("SELECT model_type,extra FROM tenant_model WHERE model_name='multi'").Scan(&primary, &raw); err != nil {
		t.Fatal(err)
	}
	var extra struct {
		ModelTypes []string `json:"model_types"`
		Thinking   *bool    `json:"thinking"`
	}
	if err := json.Unmarshal([]byte(raw), &extra); err != nil || primary != "chat" || !reflect.DeepEqual(extra.ModelTypes, []string{"chat", "image2text", "embedding"}) || extra.Thinking == nil || *extra.Thinking {
		t.Fatalf("multi-capability SQL: %s %s %v", primary, raw, err)
	}
	if _, err := svc.GetChatModel("owner-tenant", "multi@local-0@vllm"); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.GetEmbeddingModel("owner-tenant", "multi@local-0@vllm"); err != nil {
		t.Fatal(err)
	}
	rows, err := svc.ListInstanceModels("vllm", "local-0", "owner")
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, row := range rows {
		if row["name"] == "multi" {
			found = true
			if !reflect.DeepEqual(row["model_types"], extra.ModelTypes) || row["model_type"] != "chat" {
				t.Fatal(row)
			}
		}
	}
	if !found {
		t.Fatal("multi model missing")
	}
	// Legacy Extra remains readable without changing the old single-type meaning.
	if err := dao.DB.Model(&entity.TenantModel{}).Where("model_name = ?", "multi").Update("extra", `{"max_tokens":1024}`).Error; err != nil {
		t.Fatal(err)
	}
	if _, err := svc.GetChatModel("owner-tenant", "multi@local-0@vllm"); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.GetEmbeddingModel("owner-tenant", "multi@local-0@vllm"); err == nil {
		t.Fatal("legacy row acquired undeclared capability")
	}
	if err := dao.DB.Model(&entity.TenantModel{}).Where("model_name = ?", "multi").Update("extra", raw).Error; err != nil {
		t.Fatal(err)
	}
	// Matching provider/instance/model names in another tenant must remain untouched.
	if err := dao.DB.Create(&entity.TenantModelProvider{ID: "other-provider", ProviderName: "vllm", TenantID: "other-tenant"}).Error; err != nil {
		t.Fatal(err)
	}
	if err := dao.DB.Create(&entity.TenantModelInstance{ID: "other-instance", ProviderID: "other-provider", InstanceName: "local-0", APIKey: "other-fixture", Status: "active", Extra: "{}"}).Error; err != nil {
		t.Fatal(err)
	}
	if code, err := svc.AddCustomModel(&service.AddCustomModelRequest{ProviderName: "vllm", InstanceName: "local-0", ModelName: "multi", ModelType: "chat", MaxTokens: 1024}, "other"); code != common.CodeSuccess || err != nil {
		t.Fatalf("other declaration %d %v", code, err)
	}
	before := count("tenant_model", "owner-tenant")
	for _, names := range [][]string{nil, {}, {""}, {" "}} {
		check("DELETE", path, "owner", map[string]interface{}{"models": names}, common.CodeBadRequest)
	}
	status, _ := request("DELETE", path, "", map[string]interface{}{"models": []string{"multi"}})
	if status != http.StatusUnauthorized {
		t.Fatal("unauthenticated deletion allowed")
	}
	check("DELETE", path, "other", map[string]interface{}{"models": []string{"Qwen/custom"}}, common.CodeNotFound)
	check("DELETE", path, "owner", map[string]interface{}{"models": []string{"multi", "zzz-missing"}}, common.CodeNotFound)
	if count("tenant_model", "owner-tenant") != before {
		t.Fatal("partial batch committed")
	}
	check("POST", path, "owner", map[string]interface{}{"model_name": "z-fail", "model_type": "chat", "max_tokens": 1024}, common.CodeSuccess)
	// Inject a real SQL failure after the first delete, requiring transaction rollback.
	if _, err := db.Exec(`CREATE FUNCTION fail_model_delete() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF OLD.model_name='z-fail' THEN RAISE EXCEPTION 'secret-delete'; END IF; RETURN OLD; END $$; CREATE TRIGGER fail_model_delete BEFORE DELETE ON tenant_model FOR EACH ROW EXECUTE FUNCTION fail_model_delete()`); err != nil {
		t.Fatal(err)
	}
	check("DELETE", path, "owner", map[string]interface{}{"models": []string{"multi", "z-fail"}}, common.CodeServerError)
	if count("tenant_model", "owner-tenant") != before+1 {
		t.Fatal("SQL failure partially committed")
	}
	if _, err := db.Exec(`DROP TRIGGER fail_model_delete ON tenant_model; DROP FUNCTION fail_model_delete()`); err != nil {
		t.Fatal(err)
	}
	check("DELETE", path, "owner", map[string]interface{}{"models": []string{"multi", "multi", "z-fail"}}, common.CodeSuccess)
	if count("tenant_model", "owner-tenant") != before-1 || count("tenant_model", "other-tenant") != 1 {
		t.Fatal("deletion scope or duplicate handling failed")
	}
	if _, err := svc.GetChatModel("owner-tenant", "multi@local-0@vllm"); err == nil {
		t.Fatal("deleted declaration still resolves")
	}
	check("DELETE", path, "owner", map[string]interface{}{"models": []string{"multi"}}, common.CodeNotFound)
	// Instance cleanup is one transaction including children and all selected instances.
	route := "/api/v1/providers/vllm/instances"
	before = count("tenant_model", "owner-tenant")
	check("DELETE", route, "owner", map[string]interface{}{"instances": []string{"local-0", "zzz-missing"}}, common.CodeNotFound)
	if count("tenant_model", "owner-tenant") != before || count("tenant_model_instance", "owner-tenant") != 2 {
		t.Fatal("missing instance partially committed")
	}
	if _, err := db.Exec(`CREATE FUNCTION fail_instance_delete() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF OLD.instance_name='local-1' THEN RAISE EXCEPTION 'secret-delete'; END IF; RETURN OLD; END $$; CREATE TRIGGER fail_instance_delete BEFORE DELETE ON tenant_model_instance FOR EACH ROW EXECUTE FUNCTION fail_instance_delete()`); err != nil {
		t.Fatal(err)
	}
	check("DELETE", route, "owner", map[string]interface{}{"instances": []string{"local-0", "local-1"}}, common.CodeServerError)
	if count("tenant_model", "owner-tenant") != before || count("tenant_model_instance", "owner-tenant") != 2 {
		t.Fatal("instance SQL failure partially committed")
	}
	if _, err := db.Exec(`DROP TRIGGER fail_instance_delete ON tenant_model_instance; DROP FUNCTION fail_instance_delete()`); err != nil {
		t.Fatal(err)
	}
	check("DELETE", route, "owner", map[string]interface{}{"instances": []string{"local-0", "local-1", "local-1"}}, common.CodeSuccess)
	if count("tenant_model", "owner-tenant") != 0 || count("tenant_model_instance", "owner-tenant") != 0 || count("tenant_model", "other-tenant") != 1 || count("tenant_model_instance", "other-tenant") != 1 {
		t.Fatal("orphan models or cross-tenant deletion")
	}
}

func exerciseCatalogDeletionRace(t *testing.T, svc *service.ModelProviderService, db *sql.DB) {
	t.Helper()
	if code, err := svc.AddModelProvider("Google", "owner"); code != common.CodeSuccess || err != nil {
		t.Fatalf("Google provider %d %v", code, err)
	}
	var provider entity.TenantModelProvider
	if err := dao.DB.Where("tenant_id = ? AND provider_name = ?", "owner-tenant", "Google").First(&provider).Error; err != nil {
		t.Fatal(err)
	}
	instance := entity.TenantModelInstance{ID: "catalog-instance", ProviderID: provider.ID, InstanceName: "catalog", APIKey: "catalog-fixture", Status: "active", Extra: "{}"}
	if err := dao.DB.Create(&instance).Error; err != nil {
		t.Fatal(err)
	}
	if code, err := svc.UpdateModelStatus("Google", "catalog", "gemini-2.5-flash", "owner", "disable"); code != common.CodeSuccess || err != nil {
		t.Fatalf("catalog disable %d %v", code, err)
	}
	if code, err := svc.DropInstanceModels("Google", "catalog", "owner", []string{"gemini-2.5-flash"}); code != common.CodeBadRequest || err == nil {
		t.Fatalf("catalog marker deletion %d %v", code, err)
	}
	if _, err := svc.GetChatModel("owner-tenant", "gemini-2.5-flash@catalog@Google"); err == nil {
		t.Fatal("DELETE re-enabled catalog model")
	}
	// Hold the instance lock so declaration, status write and deletion overlap.
	tx := dao.DB.Begin()
	if tx.Error != nil {
		t.Fatal(tx.Error)
	}
	defer tx.Rollback()
	if err := tx.Exec("SELECT id FROM tenant_model_instance WHERE id = ? FOR UPDATE", instance.ID).Error; err != nil {
		t.Fatal(err)
	}
	type outcome struct {
		code     common.ErrorCode
		err      error
		deletion bool
	}
	outcomes := make(chan outcome, 3)
	started := make(chan struct{}, 3)
	go func() {
		started <- struct{}{}
		code, err := svc.AddCustomModel(&service.AddCustomModelRequest{ProviderName: "Google", InstanceName: "catalog", ModelName: "race-model", ModelType: "chat", MaxTokens: 100}, "owner")
		outcomes <- outcome{code, err, false}
	}()
	go func() {
		started <- struct{}{}
		code, err := svc.UpdateModelStatus("Google", "catalog", "gemini-2.5-flash", "owner", "disable")
		outcomes <- outcome{code, err, false}
	}()
	go func() {
		started <- struct{}{}
		code, err := svc.DropProviderInstances("Google", "owner", []string{"catalog"})
		outcomes <- outcome{code, err, true}
	}()
	for range 3 {
		<-started
	}
	if err := tx.Commit().Error; err != nil {
		t.Fatal(err)
	}
	for range 3 {
		select {
		case result := <-outcomes:
			if result.deletion && (result.code != common.CodeSuccess || result.err != nil) {
				t.Fatalf("delete race %v", result)
			}
			if (result.code == common.CodeSuccess) != (result.err == nil) {
				t.Fatalf("inconsistent success %v", result)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("instance mutation deadlocked")
		}
	}
	var remaining int
	if err := db.QueryRow("SELECT count(*) FROM tenant_model WHERE provider_id=$1", provider.ID).Scan(&remaining); err != nil || remaining != 0 {
		t.Fatalf("orphan models %d %v", remaining, err)
	}
	if err := db.QueryRow("SELECT count(*) FROM tenant_model_instance WHERE provider_id=$1", provider.ID).Scan(&remaining); err != nil || remaining != 0 {
		t.Fatalf("instance not deleted %d %v", remaining, err)
	}
}
