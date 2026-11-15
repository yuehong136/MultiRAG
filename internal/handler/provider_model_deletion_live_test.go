package handler_test

import (
	"multirag/internal/dao"
	"multirag/internal/entity"
	"testing"
)

func TestCanonicalProviderDeletionAtomicHTTPAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	ids := map[string]string{}
	for _, instance := range []string{"one", "two"} {
		for _, model := range []string{"first", "blocked"} {
			ids[instance+model] = f.enroll(t, "vllm", instance, "owned-key", "http://127.0.0.1:1", model, "chat")
		}
	}
	// A foreign provider with the same names is never affected by owner mutation.
	var p entity.TenantModelProvider
	if err := dao.DB.Where("tenant_id=? AND provider_name=?", "owner-tenant", "vllm").First(&p).Error; err != nil {
		t.Fatal(err)
	}
	if err := dao.DB.Create(&entity.TenantModelProvider{ID: "foreign-provider", TenantID: "foreign-tenant", ProviderName: "vllm"}).Error; err != nil {
		t.Fatal(err)
	}
	if err := dao.DB.Create(&entity.TenantModelInstance{ID: "foreign-instance", ProviderID: "foreign-provider", InstanceName: "one", APIKey: "foreign-private", Extra: `{}`}).Error; err != nil {
		t.Fatal(err)
	}
	if err := dao.DB.Create(&entity.TenantModel{ID: "foreign-model", ProviderID: "foreign-provider", InstanceID: "foreign-instance", ModelName: "first", ModelType: "chat", Status: "active", Extra: `{}`}).Error; err != nil {
		t.Fatal(err)
	}
	path := "/api/v1/providers/vllm/instances/one/models"
	assertFailureUnchanged := func(actor, method, path string, body any) {
		t.Helper()
		before := f.snapshot(t)
		_, result, _ := f.request(t, actor, method, path, body)
		if providerEnvelopeError(result) == nil || f.snapshot(t) != before {
			t.Fatal("failed mutation committed SQL", actor, method, path)
		}
	}
	assertFailureUnchanged("", "DELETE", path, map[string]any{"models": []string{"first"}})
	// Foreign actor cannot select owner UUIDs by declaration names; only its own scope can change.
	beforeForeign := f.snapshot(t)
	_, _, _ = f.request(t, "inactive", "DELETE", path, map[string]any{"models": []string{"first"}})
	if f.snapshot(t) != beforeForeign {
		t.Fatal("inactive deletion changed SQL")
	}
	assertFailureUnchanged("owner", "DELETE", path, map[string]any{"models": []string{"first", "missing"}})
	// Deliberate physical MySQL failure proves earlier deletions roll back.
	if err := dao.DB.Exec("CREATE TRIGGER provider_reject_model_delete BEFORE DELETE ON tenant_model FOR EACH ROW BEGIN IF OLD.model_name='blocked' THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='controlled deletion failure'; END IF; END").Error; err != nil {
		t.Fatal(err)
	}
	assertFailureUnchanged("owner", "DELETE", path, map[string]any{"models": []string{"first", "blocked"}})
	assertFailureUnchanged("owner", "DELETE", "/api/v1/providers/vllm/instances", map[string]any{"instances": []string{"one", "two"}})
	assertFailureUnchanged("owner", "DELETE", "/api/v1/providers/vllm", nil)
	if err := dao.DB.Exec("DROP TRIGGER provider_reject_model_delete").Error; err != nil {
		t.Fatal(err)
	}
	f.requireSuccess(t, "owner", "DELETE", path, map[string]any{"models": []string{"first", "first"}})
	var n int
	if err := f.Read.QueryRow("SELECT count(*) FROM tenant_model WHERE id=?", ids["onefirst"]).Scan(&n); err != nil || n != 0 {
		t.Fatal("deleted model persists", err)
	}
	if err := f.Read.QueryRow("SELECT count(*) FROM tenant_model WHERE id='foreign-model'").Scan(&n); err != nil || n != 1 {
		t.Fatal("foreign model deleted", err)
	}
	f.requireSuccess(t, "owner", "DELETE", "/api/v1/providers/vllm/instances", map[string]any{"instances": []string{"one", "two", "missing"}})
	for _, table := range []string{"tenant_model", "tenant_model_instance"} {
		if err := f.Read.QueryRow("SELECT count(*) FROM "+table+" WHERE provider_id=?", p.ID).Scan(&n); err != nil || n != 0 {
			t.Fatal("cascade left child rows", table, err)
		}
	}
	f.requireSuccess(t, "owner", "DELETE", "/api/v1/providers/vllm", nil)
	if err := f.Read.QueryRow("SELECT count(*) FROM tenant_model_provider WHERE id=?", p.ID).Scan(&n); err != nil || n != 0 {
		t.Fatal("provider retained", err)
	}
	if err := f.Read.QueryRow("SELECT count(*) FROM tenant_model_provider WHERE id='foreign-provider'").Scan(&n); err != nil || n != 1 {
		t.Fatal("foreign provider changed", err)
	}
}
