package admin

import (
	"encoding/json"
	"multirag/internal/entity"
	"strings"
	"testing"
)

func seedDeleteUser(t *testing.T, f *adminFixture, who string) {
	t.Helper()
	status := "1"
	title := "agent"
	llmName, kind, key := "model", "chat", "legacy-secret"
	no := false
	records := []any{&entity.User{ID: who, Email: who + "@delete.invalid", Nickname: who, IsActive: "0", Status: &status, IsSuperuser: &no}, &entity.Tenant{ID: who, Status: &status}, &entity.UserTenant{ID: who, UserID: who, TenantID: who, Role: "owner", Status: &status}, &entity.TenantModelProvider{ID: who, ProviderName: "OpenAI", TenantID: who}, &entity.TenantModelInstance{ID: who, ProviderID: who, InstanceName: "default", APIKey: "retained-secret"}, &entity.TenantModel{ID: who, ProviderID: who, InstanceID: who, ModelName: "model", ModelType: "chat"}, &entity.TenantModelGroup{ID: who, GroupType: "chat"}, &entity.TenantModelGroupMapping{GroupID: who, ProviderID: who, InstanceID: who, ModelID: who}, &entity.File{ID: who, ParentID: who, TenantID: who, CreatedBy: who, Name: "/", Type: "folder"}, &entity.UserCanvas{ID: who, UserID: who, Title: &title}, &entity.TenantLLM{TenantID: who, LLMFactory: "OpenAI", LLMName: &llmName, ModelType: &kind, APIKey: &key}}
	for _, record := range records {
		if err := f.DB.Create(record).Error; err != nil {
			t.Fatal(err)
		}
	}
}
func assertDeleteRows(t *testing.T, f *adminFixture, who string, want int) {
	t.Helper()
	for _, table := range []string{"user", "tenant", "user_tenant", "file", "user_canvas", "tenant_model_provider", "tenant_model_instance", "tenant_model"} {
		var count int
		if err := f.Read.QueryRow("SELECT count(*) FROM "+table+" WHERE id=?", who).Scan(&count); err != nil || count != want {
			t.Fatalf("%s/%s count=%d want=%d err=%v", table, who, count, want, err)
		}
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM tenant_model_group_mapping WHERE provider_id=? AND group_id=provider_id", who).Scan(&count); err != nil || count != want {
		t.Fatal("mapping side effects", count, err)
	}
}
func TestAfterFFAdminDeleteUserModernCredentialsSQL(t *testing.T) {
	f := newAdminFixture(t)
	seedDeleteUser(t, f, "owned")
	seedDeleteUser(t, f, "foreign")
	for _, id := range []string{"shared", "empty"} {
		if err := f.DB.Create(&entity.TenantModelGroup{ID: id, GroupType: "chat"}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, who := range []string{"owned", "foreign"} {
		if err := f.DB.Create(&entity.TenantModelGroupMapping{GroupID: "shared", ProviderID: who, InstanceID: who, ModelID: who}).Error; err != nil {
			t.Fatal(err)
		}
	}
	beforeForeign := adminDeletionSnapshot(t, f, "foreign")
	result, err := f.service.DeleteUser("owned@delete.invalid")
	if err != nil || result == nil {
		t.Fatal(err)
	}
	assertDeleteRows(t, f, "owned", 0)
	assertDeleteRows(t, f, "foreign", 1)
	if adminDeletionSnapshot(t, f, "foreign") != beforeForeign {
		t.Fatal("foreign credential bytes changed")
	}
	for _, id := range []string{"shared", "empty"} {
		var count int
		if err := f.Read.QueryRow("SELECT count(*) FROM tenant_model_group WHERE id=?", id).Scan(&count); err != nil || count != 1 {
			t.Fatal("shared/unrelated group deleted", id, err)
		}
	}
	if strings.Contains(strings.Join(result.DeletedDetails, " "), "Deleted metadata table") {
		t.Fatal("unperformed metadata drop reported success")
	}
	// Delete groups formerly exclusive to removed mappings; preserve shared and unrelated empty groups.
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM tenant_model_group WHERE id=?", "owned").Scan(&count); err != nil || count != 0 {
		t.Fatal("owned group retained", err)
	}
}
func adminDeletionSnapshot(t *testing.T, f *adminFixture, who string) string {
	t.Helper()
	var result []any
	for _, table := range []string{"user", "tenant", "user_tenant", "file", "user_canvas", "tenant_model_provider", "tenant_model_instance", "tenant_model", "tenant_model_group_mapping", "tenant_llm"} {
		column := "id"
		if table == "tenant_model_group_mapping" {
			column = "provider_id"
		}
		if table == "tenant_llm" {
			column = "tenant_id"
		}
		order := "id"
		if table == "tenant_model_group_mapping" {
			order = "group_id,provider_id,instance_id,model_id"
		}
		rows, err := f.Read.Query("SELECT * FROM "+table+" WHERE "+column+"=? ORDER BY "+order, who)
		if err != nil {
			t.Fatal(err)
		}
		columns, err := rows.Columns()
		if err != nil {
			t.Fatal(err)
		}
		var values []any
		for rows.Next() {
			row := make([]any, len(columns))
			pointers := make([]any, len(row))
			for i := range row {
				pointers[i] = &row[i]
			}
			if err := rows.Scan(pointers...); err != nil {
				t.Fatal(err)
			}
			for i, value := range row {
				if raw, ok := value.([]byte); ok {
					row[i] = string(raw)
				}
			}
			values = append(values, row)
		}
		if err := rows.Err(); err != nil {
			t.Fatal(err)
		}
		rows.Close()
		result = append(result, []any{table, values})
	}
	raw, err := json.Marshal(result)
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}
func TestAfterFFAdminDeleteUserModelFailureRollsBackSQL(t *testing.T) {
	for _, table := range []string{"tenant_model", "tenant_model_instance", "user_tenant", "file"} {
		t.Run(table, func(t *testing.T) {
			f := newAdminFixture(t)
			seedDeleteUser(t, f, "owned")
			seedDeleteUser(t, f, "foreign")
			before := adminDeletionSnapshot(t, f, "owned")
			foreign := adminDeletionSnapshot(t, f, "foreign")
			if err := f.DB.Exec("CREATE TRIGGER reject_admin_delete BEFORE DELETE ON " + table + " FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='controlled delete failure'").Error; err != nil {
				t.Fatal(err)
			}
			result, err := f.service.DeleteUser("owned@delete.invalid")
			if err == nil || result != nil {
				t.Fatal("partial deletion accepted")
			}
			assertDeleteRows(t, f, "owned", 1)
			assertDeleteRows(t, f, "foreign", 1)
			if adminDeletionSnapshot(t, f, "owned") != before || adminDeletionSnapshot(t, f, "foreign") != foreign {
				t.Fatal("failed transaction changed persisted rows")
			}
		})
	}
}

// Late provider exclusion is exercised against the target provider methods by the model owner.
func TestAdminDeleteUserActiveAndRepeatedDenied(t *testing.T) {
	f := newAdminFixture(t)
	seedDeleteUser(t, f, "owned")
	var active entity.User
	if err := f.DB.Where("id = ?", "admin").First(&active).Error; err != nil {
		t.Fatal(err)
	}
	if result, err := f.service.DeleteUser(active.Email); err == nil || result != nil {
		t.Fatal("active admin deleted")
	}
	if result, err := f.service.DeleteUser("owned@delete.invalid"); err != nil || result == nil {
		t.Fatal(err)
	}
	if result, err := f.service.DeleteUser("owned@delete.invalid"); err == nil || result != nil {
		t.Fatal("repeated deletion fabricated success")
	}
	assertDeleteRows(t, f, "owned", 0)
}
