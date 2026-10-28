package admin

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/google/uuid"
	"gorm.io/gorm"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/service"
	"multirag/internal/testsupport"
)

// TestAfterFFAdminRegistrationSQL proves that retiring the old catalog loader
// leaves the active admin registration consumer on the same transactional model
// initialization contract as public registration. No provider is called.
func TestAfterFFAdminRegistrationSQL(t *testing.T) {
	journal := testsupport.NewCase(t)
	config := os.Getenv("MULTIRAG_AFTERFF_G_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned afterff G PostgreSQL config required")
		}
		t.Skip("requires owned afterff G PostgreSQL config")
	}
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal("owned configuration unavailable")
	}
	cfg := server.GetConfig()
	if cfg.Database.Host != "127.0.0.1" || cfg.Database.Port != 5432 || cfg.Database.Database != "multirag_batch_g_afterff_20261028" || cfg.Database.Schema != "usr_ai" {
		t.Fatal("exact owned PostgreSQL target required")
	}
	journal.Event("reserve-schema-before-SQL", map[string]any{"database": cfg.Database.Database, "schema": cfg.Database.Schema, "host": cfg.Database.Host, "port": cfg.Database.Port})
	if err := dao.InitDB(); err != nil {
		t.Fatal("owned schema initialization failed")
	}
	pool, err := dao.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = pool.Close() })
	dsn, err := dao.PostgresDSN(cfg.Database)
	if err != nil {
		t.Fatal(err)
	}
	read, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("independent SQL connection failed")
	}
	t.Cleanup(func() { _ = read.Close() })
	if err := read.PingContext(context.Background()); err != nil {
		t.Fatal("independent SQL readback unavailable")
	}

	suffix := os.Getenv("MULTIRAG_RUN_ID")
	if len(suffix) < 12 {
		suffix = strings.ReplaceAll(uuid.NewString(), "-", "")
	}
	suffix = suffix[:12]
	successEmail, failedEmail := "gff-admin-ok-"+suffix+"@example.invalid", "gff-admin-fail-"+suffix+"@example.invalid"
	callbackName := "afterff_admin_resources_" + suffix
	rowIDs := map[string][]string{}
	if err := dao.DB.Callback().Create().Before("gorm:create").Register(callbackName, func(tx *gorm.DB) {
		value := tx.Statement.ReflectValue
		if value.Kind() == reflect.Pointer {
			value = value.Elem()
		}
		var ids []string
		appendID := func(v reflect.Value) {
			if v.Kind() == reflect.Pointer {
				v = v.Elem()
			}
			if v.Kind() == reflect.Struct {
				field := v.FieldByName("ID")
				if field.IsValid() {
					ids = append(ids, fmt.Sprint(field.Interface()))
				}
			}
		}
		if value.Kind() == reflect.Slice || value.Kind() == reflect.Array {
			for i := 0; i < value.Len(); i++ {
				appendID(value.Index(i))
			}
		} else {
			appendID(value)
		}
		journal.Event("reserve-rows-before-SQL", map[string]any{"table": tx.Statement.Table, "ids": ids})
		rowIDs[tx.Statement.Table] = append(rowIDs[tx.Statement.Table], ids...)
	}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = dao.DB.Callback().Create().Remove(callbackName) })
	// Install cleanup before the first application write; rolled-back identities
	// remain in the journal and are independently confirmed absent as well.
	t.Cleanup(func() {
		for _, table := range []string{"tenant_model", "tenant_model_instance", "tenant_model_provider", "t_ai_tenant_llms", "t_ai_files", "t_ai_user_tenants", "t_ai_tenants", "t_ai_users"} {
			ids := rowIDs[table]
			if len(ids) == 0 {
				continue
			}
			marks := make([]string, len(ids))
			args := make([]any, len(ids))
			for i, id := range ids {
				marks[i] = fmt.Sprintf("$%d", i+1)
				args[i] = id
			}
			condition := "id IN (" + strings.Join(marks, ",") + ")"
			if _, err := read.Exec("DELETE FROM "+table+" WHERE "+condition, args...); err != nil {
				t.Error("owned registration rows cleanup failed")
			}
			var remaining int
			err := read.QueryRow("SELECT count(*) FROM "+table+" WHERE "+condition, args...).Scan(&remaining)
			journal.Event("cleanup-registration-SQL-readback", map[string]any{"table": table, "ids": ids, "remaining": remaining, "confirmed": err == nil && remaining == 0})
			if err != nil || remaining != 0 {
				t.Error("owned registration rows remain")
			}
		}
	})
	previousDefaults := cfg.UserDefaultLLM.DefaultModels
	cfg.UserDefaultLLM.DefaultModels = server.DefaultModelsConfig{ASRModel: server.ModelConfig{Factory: "Groq", Name: "whisper-large-v3", APIKey: "owned-admin-asr-fixture", BaseURL: "https://owned.invalid/v1"}}
	t.Cleanup(func() { cfg.UserDefaultLLM.DefaultModels = previousDefaults })
	definition, err := dao.GetModelProviderManager().GetModelByName("Groq", "whisper-large-v3")
	if err != nil || definition.MaxTokens != 0 {
		t.Fatal("zero-limit ASR catalog fixture changed")
	}

	snapshot := func() string {
		t.Helper()
		var parts []string
		for _, table := range []string{"t_ai_users", "t_ai_tenants", "t_ai_user_tenants", "tenant_model_provider", "tenant_model_instance", "tenant_model", "t_ai_tenant_llms", "t_ai_files"} {
			var rows string
			if err := read.QueryRow("SELECT COALESCE(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text)::text,'[]') FROM " + table + " x").Scan(&rows); err != nil {
				t.Fatal("independent registration snapshot failed")
			}
			parts = append(parts, rows)
		}
		return fmt.Sprintf("%x", sha256.Sum256([]byte(strings.Join(parts, "\n"))))
	}
	functionName, triggerName := "gff_admin_root_failure_"+suffix, "gff_admin_root_trigger_"+suffix
	journal.Event("reserve-SQL-function-trigger", map[string]any{"function": functionName, "trigger": triggerName, "table": "t_ai_files", "email": failedEmail})
	if _, err := read.Exec("CREATE FUNCTION " + functionName + "() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.name='/' AND EXISTS (SELECT 1 FROM t_ai_users WHERE id=NEW.tenant_id AND email='" + failedEmail + "') THEN RAISE EXCEPTION 'owned-root-failure'; END IF; RETURN NEW; END $$"); err != nil {
		t.Fatal("owned failure function creation failed")
	}
	triggerCreated := false
	t.Cleanup(func() {
		if triggerCreated {
			if _, err := read.Exec("DROP TRIGGER IF EXISTS " + triggerName + " ON t_ai_files"); err != nil {
				t.Error("owned root trigger cleanup failed")
			}
		}
		if _, err := read.Exec("DROP FUNCTION IF EXISTS " + functionName + "()"); err != nil {
			t.Error("owned root function cleanup failed")
		}
		var remaining int
		err := read.QueryRow("SELECT (SELECT count(*) FROM pg_proc WHERE proname=$1)+(SELECT count(*) FROM pg_trigger WHERE tgname=$2)", functionName, triggerName).Scan(&remaining)
		journal.Event("cleanup-registration-failure-injection-readback", map[string]any{"function": functionName, "trigger": triggerName, "remaining": remaining, "confirmed": err == nil && remaining == 0})
		if err != nil || remaining != 0 {
			t.Error("owned registration failure injection remains")
		}
	})
	if _, err := read.Exec("CREATE TRIGGER " + triggerName + " BEFORE INSERT ON t_ai_files FOR EACH ROW EXECUTE FUNCTION " + functionName + "()"); err != nil {
		t.Fatal("owned failure trigger creation failed")
	}
	triggerCreated = true
	before := snapshot()
	if result, err := NewService().CreateUser(failedEmail, "owned-password!", "user"); err == nil || result != nil {
		t.Fatal("late registration SQL failure reported success")
	}
	if snapshot() != before {
		t.Fatal("late SQL failure left account, tenant, model, or file state")
	}
	var failedCount int
	if err := read.QueryRow("SELECT count(*) FROM t_ai_users WHERE email=$1", failedEmail).Scan(&failedCount); err != nil || failedCount != 0 {
		t.Fatal("failed registration account remains")
	}
	if _, err := read.Exec("DROP TRIGGER " + triggerName + " ON t_ai_files"); err != nil {
		t.Fatal("owned root trigger removal failed")
	}
	triggerCreated = false

	result, err := NewService().CreateUser(successEmail, "owned-password!", "user")
	if err != nil || result == nil {
		t.Fatal("admin registration with catalog-backed ASR failed")
	}
	if _, ok := result["password"]; ok {
		t.Fatal("registration exposed password")
	}
	if _, ok := result["access_token"]; ok {
		t.Fatal("registration exposed access token")
	}
	userID, ok := result["id"].(string)
	if !ok || userID == "" {
		t.Fatal("admin registration missing account identity")
	}
	var selector string
	if err := read.QueryRow("SELECT asr_id FROM t_ai_tenants WHERE id=$1", userID).Scan(&selector); err != nil || selector != "whisper-large-v3@default-asr@Groq" {
		t.Fatal("admin default ASR selector was not initialized")
	}
	var providerID, instanceID, storedKey, instanceRaw string
	if err := read.QueryRow("SELECT p.id,i.id,i.api_key,i.extra FROM tenant_model_provider p JOIN tenant_model_instance i ON i.provider_id=p.id WHERE p.tenant_id=$1 AND p.provider_name='Groq' AND i.instance_name='default-asr'", userID).Scan(&providerID, &instanceID, &storedKey, &instanceRaw); err != nil {
		t.Fatal("admin provider and instance SQL readback failed")
	}
	if storedKey != "owned-admin-asr-fixture" {
		t.Fatal("admin default instance credentials changed")
	}
	var instanceExtra struct {
		Region  string `json:"region"`
		BaseURL string `json:"base_url"`
	}
	if json.Unmarshal([]byte(instanceRaw), &instanceExtra) != nil || instanceExtra.Region != "default" || instanceExtra.BaseURL != "https://owned.invalid/v1" {
		t.Fatal("admin default instance endpoint changed")
	}
	var customCount, legacyCount, rootCount, ownerCount int
	if err := read.QueryRow("SELECT count(*) FROM tenant_model WHERE provider_id=$1", providerID).Scan(&customCount); err != nil || customCount != 0 {
		t.Fatal("zero-limit catalog model became a disabled or custom override")
	}
	if err := read.QueryRow("SELECT count(*) FROM t_ai_tenant_llms WHERE tenant_id=$1", userID).Scan(&legacyCount); err != nil || legacyCount != 0 {
		t.Fatal("admin registration still depends on legacy model rows")
	}
	if err := read.QueryRow("SELECT count(*) FROM t_ai_files WHERE tenant_id=$1 AND created_by=$1 AND name='/' AND type='folder' AND id=parent_id", userID).Scan(&rootCount); err != nil || rootCount != 1 {
		t.Fatal("admin registration root folder missing")
	}
	if err := read.QueryRow("SELECT count(*) FROM t_ai_user_tenants WHERE user_id=$1 AND tenant_id=$1 AND role='owner' AND status='1'", userID).Scan(&ownerCount); err != nil || ownerCount != 1 {
		t.Fatal("admin registration owner membership missing")
	}

	tenants := service.NewTenantService()
	resolvedDefault, err := tenants.GetDefaultModelName(userID, entity.ModelTypeSpeech2Text)
	if err != nil || resolvedDefault != selector {
		t.Fatal("admin ASR default could not be selected")
	}
	provider, instance, name, enabled, err := tenants.GetModelInfo(userID, selector, string(entity.ModelTypeSpeech2Text))
	if err != nil || !enabled || provider == nil || instance == nil || name == nil || *provider != "Groq" || *instance != "default-asr" || *name != "whisper-large-v3" {
		t.Fatal("catalog-backed zero ASR resolution failed")
	}
	listed, err := service.NewModelProviderService().ListInstanceModels("Groq", "default-asr", userID)
	if err != nil {
		t.Fatal("initialized ASR inventory could not be read")
	}
	foundZero := false
	for _, row := range listed {
		if row["name"] == "whisper-large-v3" {
			foundZero = row["max_tokens"] == 0 && row["status"] == "active"
		}
	}
	if !foundZero || definition.MaxTokens != 0 {
		t.Fatal("catalog-backed ASR zero limit changed")
	}
	journal.Event("admin-registration-contract-readback", map[string]any{"user_id": userID, "provider_id": providerID, "instance_id": instanceID, "selector": selector, "catalog_max_tokens": 0, "custom_overrides": customCount, "legacy_rows": legacyCount, "root_folders": rootCount, "owner_memberships": ownerCount, "late_failure_rolled_back": true, "provider_IO": false})
}
