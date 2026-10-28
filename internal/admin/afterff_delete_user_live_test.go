package admin

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"gorm.io/gorm"
	gormlog "gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/service"
	"multirag/internal/testsupport"
)

type afterFFDeleteFixture struct {
	t                                                                                 *testing.T
	read                                                                              *sql.DB
	journal                                                                           *testsupport.CaseJournal
	service                                                                           *Service
	prefix, target, other, targetEmail, otherEmail, ownGroup, sharedGroup, emptyGroup string
	mu                                                                                sync.Mutex
	rows                                                                              map[string][]string
	targetProviders, otherProviders                                                   []string
}

func afterFFDeleteUserFixture(t *testing.T) *afterFFDeleteFixture {
	t.Helper()
	journal := testsupport.NewCase(t)
	config := os.Getenv("MULTIRAG_AFTERFF_G_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned admin deletion SQL configuration required")
		}
		t.Skip("owned admin deletion SQL configuration required")
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
		t.Fatal("exact owned SQL endpoint required")
	}
	journal.Event("reserve-admin-delete-schema", map[string]any{"database": cfg.Database.Database, "schema": cfg.Database.Schema, "host": cfg.Database.Host, "port": cfg.Database.Port})
	previousDB := dao.DB
	if err := dao.InitDB(); err != nil {
		t.Fatal("owned SQL schema unavailable")
	}
	dao.DB = dao.DB.Session(&gorm.Session{Logger: gormlog.Default.LogMode(gormlog.Silent)})
	pool, err := dao.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close(); dao.DB = previousDB })
	dsn, err := dao.PostgresDSN(cfg.Database)
	if err != nil {
		t.Fatal(err)
	}
	read, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("independent SQL unavailable")
	}
	t.Cleanup(func() { read.Close() })
	if err := read.PingContext(context.Background()); err != nil {
		t.Fatal("independent SQL unavailable")
	}
	f := &afterFFDeleteFixture{t: t, read: read, journal: journal, service: NewService(), prefix: "gdu" + strings.ReplaceAll(uuid.NewString(), "-", "")[:15], rows: map[string][]string{}}
	f.targetEmail, f.otherEmail = f.prefix+"target@example.invalid", f.prefix+"other@example.invalid"
	callback := f.prefix + "_register"
	if err := dao.DB.Callback().Create().Before("gorm:create").Register(callback, func(tx *gorm.DB) {
		value := tx.Statement.ReflectValue
		var ids []string
		var composite []map[string]string
		var collect func(reflect.Value)
		collect = func(v reflect.Value) {
			if v.Kind() == reflect.Pointer {
				if v.IsNil() {
					return
				}
				v = v.Elem()
			}
			if v.Kind() == reflect.Slice || v.Kind() == reflect.Array {
				for i := 0; i < v.Len(); i++ {
					collect(v.Index(i))
				}
				return
			}
			if v.Kind() != reflect.Struct {
				return
			}
			if field := v.FieldByName("ID"); field.IsValid() {
				if tx.Statement.Table == "t_ai_tenant_llms" && field.Kind() == reflect.Int64 && field.Int() == 0 {
					var sequence string
					query := tx.Session(&gorm.Session{NewDB: true})
					if err := query.Raw("SELECT pg_get_serial_sequence('t_ai_tenant_llms','id')").Scan(&sequence).Error; err != nil || sequence == "" {
						tx.AddError(fmt.Errorf("owned numeric sequence unavailable"))
						return
					}
					journal.Event("reserve-admin-delete-numeric-id", map[string]any{"database": cfg.Database.Database, "sequence": sequence, "table": tx.Statement.Table})
					var id int64
					if err := query.Raw("SELECT nextval(?::regclass)", sequence).Scan(&id).Error; err != nil || id <= 0 {
						tx.AddError(fmt.Errorf("owned numeric identity reservation failed"))
						return
					}
					field.SetInt(id)
				}
				ids = append(ids, fmt.Sprint(field.Interface()))
			}
			if tx.Statement.Table == "tenant_model_group_mapping" {
				row := map[string]string{}
				for _, name := range []string{"GroupID", "ProviderID", "InstanceID", "ModelID"} {
					if field := v.FieldByName(name); field.IsValid() {
						row[name] = fmt.Sprint(field.Interface())
					}
				}
				composite = append(composite, row)
			}
		}
		collect(value)
		if tx.Error != nil {
			return
		}
		journal.Event("reserve-admin-delete-rows", map[string]any{"table": tx.Statement.Table, "ids": ids, "mapping_identities": composite})
		f.mu.Lock()
		f.rows[tx.Statement.Table] = append(f.rows[tx.Statement.Table], ids...)
		f.mu.Unlock()
	}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = dao.DB.Callback().Create().Remove(callback) })
	// Cleanup is a fallback after business assertions; deletion under test must
	// retire target rows itself, including every saved provider credential.
	t.Cleanup(func() {
		f.mu.Lock()
		rows := map[string][]string{}
		for table, ids := range f.rows {
			rows[table] = append([]string{}, ids...)
		}
		f.mu.Unlock()
		if ids := rows["tenant_model_provider"]; len(ids) > 0 {
			where, args := afterFFIDCondition("provider_id", ids)
			if _, err := read.Exec("DELETE FROM tenant_model_group_mapping WHERE "+where, args...); err != nil {
				t.Error("owned mapping cleanup failed")
			}
		}
		for _, table := range []string{"tenant_model", "tenant_model_instance", "tenant_model_provider", "tenant_model_group", "t_ai_tenant_llms", "t_ai_files", "t_ai_user_tenants", "t_ai_tenants", "t_ai_users"} {
			ids := rows[table]
			if len(ids) == 0 {
				continue
			}
			where, args := afterFFIDCondition("id", ids)
			if _, err := read.Exec("DELETE FROM "+table+" WHERE "+where, args...); err != nil {
				t.Error("owned admin deletion cleanup failed")
			}
			var remaining int
			err := read.QueryRow("SELECT count(*) FROM "+table+" WHERE "+where, args...).Scan(&remaining)
			journal.Event("admin-delete-cleanup-readback", map[string]any{"table": table, "ids": ids, "remaining": remaining, "confirmed": err == nil && remaining == 0})
			if err != nil || remaining != 0 {
				t.Error("owned admin deletion rows remain")
			}
		}
	})
	previousDefaults := cfg.UserDefaultLLM.DefaultModels
	t.Cleanup(func() { cfg.UserDefaultLLM.DefaultModels = previousDefaults })
	create := func(email, label string) string {
		t.Helper()
		key := "owned-delete-fixture-" + f.prefix + "-" + label
		cfg.UserDefaultLLM.DefaultModels = server.DefaultModelsConfig{ChatModel: server.ModelConfig{Factory: "Groq", Name: "llama-3.3-70b-versatile", APIKey: key + "-chat", BaseURL: "https://owned.invalid/v1"}, EmbeddingModel: server.ModelConfig{Factory: "SiliconFlow", Name: "Qwen/Qwen3-Embedding-0.6B", APIKey: key + "-embedding", BaseURL: "https://owned.invalid/v1"}, ASRModel: server.ModelConfig{Factory: "Groq", Name: "whisper-large-v3", APIKey: key + "-asr", BaseURL: "https://owned.invalid/v1"}}
		result, err := f.service.CreateUser(email, "owned-password!", "user")
		if err != nil || result == nil {
			t.Fatal("actual admin registration failed")
		}
		id, ok := result["id"].(string)
		if !ok || id == "" {
			t.Fatal("admin registration identity missing")
		}
		return id
	}
	f.target, f.other = create(f.targetEmail, "target"), create(f.otherEmail, "other")
	providers := func(tenant string) []string {
		t.Helper()
		var ids []string
		if err := dao.DB.Model(&entity.TenantModelProvider{}).Where("tenant_id=?", tenant).Order("id").Pluck("id", &ids).Error; err != nil {
			t.Fatal(err)
		}
		return ids
	}
	f.targetProviders, f.otherProviders = providers(f.target), providers(f.other)
	for _, id := range []string{f.target, f.other} {
		var count int
		var numeric bool
		var chat, embedding, asr string
		if err := read.QueryRow("SELECT llm_id,embd_id,asr_id,(SELECT count(*) FROM tenant_model_instance i JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.tenant_id=$1),COALESCE(tenant_llm_id,0)>0 AND COALESCE(tenant_embd_id,0)>0 AND COALESCE(tenant_asr_id,0)>0 FROM t_ai_tenants WHERE id=$1", id).Scan(&chat, &embedding, &asr, &count, &numeric); err != nil || chat == "" || embedding == "" || asr == "" || count != 3 || !numeric {
			t.Fatal("actual configured defaults/credentials missing")
		}
	}
	var own, other entity.TenantModel
	if err := dao.DB.Where("provider_id IN ?", f.targetProviders).Order("id").Take(&own).Error; err != nil {
		t.Fatal(err)
	}
	if err := dao.DB.Where("provider_id IN ?", f.otherProviders).Order("id").Take(&other).Error; err != nil {
		t.Fatal(err)
	}
	f.ownGroup, f.sharedGroup, f.emptyGroup = f.prefix+"own", f.prefix+"shared", f.prefix+"empty"
	for _, id := range []string{f.ownGroup, f.sharedGroup, f.emptyGroup} {
		if err := dao.DB.Create(&entity.TenantModelGroup{ID: id, GroupType: "chat", Strategy: "weighted"}).Error; err != nil {
			t.Fatal(err)
		}
	}
	mappings := []entity.TenantModelGroupMapping{{GroupID: f.ownGroup, ProviderID: own.ProviderID, InstanceID: own.InstanceID, ModelID: own.ID, Weight: 70, Status: "active"}, {GroupID: f.sharedGroup, ProviderID: own.ProviderID, InstanceID: own.InstanceID, ModelID: own.ID, Weight: 40, Status: "active"}, {GroupID: f.sharedGroup, ProviderID: other.ProviderID, InstanceID: other.InstanceID, ModelID: other.ID, Weight: 60, Status: "active"}}
	if err := dao.DB.Create(&mappings).Error; err != nil {
		t.Fatal(err)
	}
	return f
}

func afterFFIDCondition(column string, ids []string) (string, []any) {
	marks := make([]string, len(ids))
	args := make([]any, len(ids))
	for i, id := range ids {
		marks[i] = fmt.Sprintf("$%d", i+1)
		args[i] = id
	}
	return column + " IN (" + strings.Join(marks, ",") + ")", args
}

func (f *afterFFDeleteFixture) snapshot() string {
	f.t.Helper()
	f.mu.Lock()
	rows := map[string][]string{}
	for table, ids := range f.rows {
		rows[table] = append([]string{}, ids...)
	}
	f.mu.Unlock()
	var parts []string
	for _, table := range []string{"t_ai_users", "t_ai_tenants", "t_ai_user_tenants", "t_ai_files", "t_ai_tenant_llms", "tenant_model_provider", "tenant_model_instance", "tenant_model", "tenant_model_group"} {
		ids := rows[table]
		if len(ids) == 0 {
			continue
		}
		where, args := afterFFIDCondition("id", ids)
		var raw string
		if err := f.read.QueryRow("SELECT COALESCE(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text)::text,'[]') FROM "+table+" x WHERE "+where, args...).Scan(&raw); err != nil {
			f.t.Fatal("independent account/model snapshot failed")
		}
		parts = append(parts, raw)
	}
	where, args := afterFFIDCondition("provider_id", rows["tenant_model_provider"])
	var raw string
	if err := f.read.QueryRow("SELECT COALESCE(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text)::text,'[]') FROM tenant_model_group_mapping x WHERE "+where, args...).Scan(&raw); err != nil {
		f.t.Fatal("independent mapping snapshot failed")
	}
	parts = append(parts, raw)
	return fmt.Sprintf("%x", sha256.Sum256([]byte(strings.Join(parts, "\n"))))
}

func (f *afterFFDeleteFixture) otherSnapshot() string {
	f.t.Helper()
	parts := []string{}
	queries := []string{"SELECT to_jsonb(x)::text FROM t_ai_users x WHERE id=$1", "SELECT to_jsonb(x)::text FROM t_ai_tenants x WHERE id=$1", "SELECT COALESCE(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text)::text,'[]') FROM t_ai_user_tenants x WHERE user_id=$1", "SELECT COALESCE(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text)::text,'[]') FROM t_ai_files x WHERE tenant_id=$1", "SELECT COALESCE(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text)::text,'[]') FROM tenant_model_provider x WHERE tenant_id=$1", "SELECT COALESCE(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text)::text,'[]') FROM tenant_model_instance x WHERE provider_id IN(SELECT id FROM tenant_model_provider WHERE tenant_id=$1)", "SELECT COALESCE(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text)::text,'[]') FROM tenant_model x WHERE provider_id IN(SELECT id FROM tenant_model_provider WHERE tenant_id=$1)", "SELECT COALESCE(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text)::text,'[]') FROM tenant_model_group_mapping x WHERE provider_id IN(SELECT id FROM tenant_model_provider WHERE tenant_id=$1)"}
	for _, query := range queries {
		var raw string
		if err := f.read.QueryRow(query, f.other).Scan(&raw); err != nil {
			f.t.Fatal("independent other tenant snapshot failed")
		}
		parts = append(parts, raw)
	}
	var legacy string
	if err := f.read.QueryRow("SELECT COALESCE(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text)::text,'[]') FROM t_ai_tenant_llms x WHERE tenant_id=$1", f.other).Scan(&legacy); err != nil {
		f.t.Fatal("independent other tenant numeric binding snapshot failed")
	}
	parts = append(parts, legacy)
	var group string
	if err := f.read.QueryRow("SELECT to_jsonb(x)::text FROM tenant_model_group x WHERE id=$1", f.sharedGroup).Scan(&group); err != nil {
		f.t.Fatal(err)
	}
	parts = append(parts, group)
	return fmt.Sprintf("%x", sha256.Sum256([]byte(strings.Join(parts, "\n"))))
}

func (f *afterFFDeleteFixture) assertRetired() {
	f.t.Helper()
	for _, table := range []string{"tenant_model", "tenant_model_instance", "tenant_model_group_mapping"} {
		where, args := afterFFIDCondition("provider_id", f.targetProviders)
		var remaining int
		if err := f.read.QueryRow("SELECT count(*) FROM "+table+" WHERE "+where, args...).Scan(&remaining); err != nil || remaining != 0 {
			f.t.Fatal("target model credentials or references remain", table, remaining)
		}
	}
	for _, table := range []string{"t_ai_users", "t_ai_tenants"} {
		var remaining int
		if err := f.read.QueryRow("SELECT count(*) FROM "+table+" WHERE id=$1", f.target).Scan(&remaining); err != nil || remaining != 0 {
			f.t.Fatal("deleted account/tenant remain", table, remaining)
		}
	}
	var legacy int
	if err := f.read.QueryRow("SELECT count(*) FROM t_ai_tenant_llms WHERE tenant_id=$1", f.target).Scan(&legacy); err != nil || legacy != 0 {
		f.t.Fatal("target numeric binding credentials remain", legacy)
	}
	where, args := afterFFIDCondition("id", f.targetProviders)
	var providers int
	if err := f.read.QueryRow("SELECT count(*) FROM tenant_model_provider WHERE "+where, args...).Scan(&providers); err != nil || providers != 0 {
		f.t.Fatal("target providers remain", providers)
	}
	var own, shared, empty int
	if err := f.read.QueryRow("SELECT (SELECT count(*) FROM tenant_model_group WHERE id=$1),(SELECT count(*) FROM tenant_model_group WHERE id=$2),(SELECT count(*) FROM tenant_model_group WHERE id=$3)", f.ownGroup, f.sharedGroup, f.emptyGroup).Scan(&own, &shared, &empty); err != nil || own != 0 || shared != 1 || empty != 1 {
		f.t.Fatal("group ownership cleanup escaped its scope", own, shared, empty)
	}
	f.journal.Event("admin-delete-business-readback", map[string]any{"user_id": f.target, "provider_ids": f.targetProviders, "modern_credentials_remaining": 0, "owned_group_rows": own, "shared_group_preserved": shared, "unrelated_empty_group_preserved": empty})
}

func TestAfterFFAdminDeleteUserModernCredentialsSQL(t *testing.T) {
	f := afterFFDeleteUserFixture(t)
	before, other := f.snapshot(), f.otherSnapshot()
	if result, err := f.service.DeleteUser(f.targetEmail); err == nil || result != nil {
		t.Fatal("active user deletion was accepted")
	}
	if f.snapshot() != before {
		t.Fatal("active deletion denial changed SQL")
	}
	if err := f.service.UpdateUserActivateStatus(f.targetEmail, false); err != nil {
		t.Fatal(err)
	}
	if code, err := service.NewModelProviderService().AddModelProvider("OpenAI", f.target); code != common.CodeSuccess || err != nil {
		t.Fatal("provider-first setup failed")
	}
	if err := dao.DB.Model(&entity.TenantModelProvider{}).Where("tenant_id=?", f.target).Order("id").Pluck("id", &f.targetProviders).Error; err != nil {
		t.Fatal(err)
	}
	if result, err := f.service.DeleteUser(f.targetEmail); err != nil || result == nil {
		t.Fatal("deactivated account deletion failed", err)
	}
	f.assertRetired()
	if f.otherSnapshot() != other {
		t.Fatal("other tenant/model/group mapping changed")
	}
}

func TestAfterFFAdminDeleteUserModelFailureRollsBackSQL(t *testing.T) {
	for _, table := range []string{"tenant_model", "tenant_model_instance", "t_ai_user_tenants"} {
		t.Run(table, func(t *testing.T) {
			f := afterFFDeleteUserFixture(t)
			if err := f.service.UpdateUserActivateStatus(f.targetEmail, false); err != nil {
				t.Fatal(err)
			}
			before := f.snapshot()
			fn, trigger := f.prefix+"_fail", f.prefix+"_trigger"
			f.journal.Event("reserve-admin-delete-trigger", map[string]any{"function": fn, "trigger": trigger, "table": table, "tenant_id": f.target})
			keyExpression := "OLD.api_key"
			guard := "OLD.provider_id IN(SELECT id FROM tenant_model_provider WHERE tenant_id='" + f.target + "')"
			if table == "tenant_model" {
				keyExpression = "(SELECT api_key FROM tenant_model_instance WHERE id=OLD.instance_id)"
			} else if table == "t_ai_user_tenants" {
				guard = "OLD.user_id='" + f.target + "'"
				keyExpression = "'controlled-late-legacy-delete-failure'"
			}
			statement := "CREATE FUNCTION " + fn + "() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF " + guard + " THEN RAISE EXCEPTION '%', " + keyExpression + "; END IF; RETURN OLD; END $$; CREATE TRIGGER " + trigger + " BEFORE DELETE ON " + table + " FOR EACH ROW EXECUTE FUNCTION " + fn + "()"
			if _, err := f.read.Exec(statement); err != nil {
				t.Fatal("owned deletion failure trigger unavailable")
			}
			t.Cleanup(func() {
				if _, err := f.read.Exec("DROP TRIGGER IF EXISTS " + trigger + " ON " + table + "; DROP FUNCTION IF EXISTS " + fn + "()"); err != nil {
					t.Error("failure trigger cleanup failed")
				}
				var remaining int
				err := f.read.QueryRow("SELECT (SELECT count(*) FROM pg_proc WHERE proname=$1)+(SELECT count(*) FROM pg_trigger WHERE tgname=$2)", fn, trigger).Scan(&remaining)
				f.journal.Event("admin-delete-trigger-cleanup-readback", map[string]any{"remaining": remaining, "confirmed": err == nil && remaining == 0})
				if err != nil || remaining != 0 {
					t.Error("failure trigger remains")
				}
			})
			result, err := f.service.DeleteUser(f.targetEmail)
			if err == nil || result != nil || strings.Contains(err.Error(), "owned-delete-fixture-") {
				t.Fatal("modern credential failure was swallowed or exposed credential")
			}
			if f.snapshot() != before {
				t.Fatal("modern cleanup failure partially deleted user/tenant/defaults/keys/groups")
			}
			var inactive bool
			if err := f.read.QueryRow("SELECT NOT is_active FROM t_ai_users WHERE id=$1", f.target).Scan(&inactive); err != nil || !inactive {
				t.Fatal("deactivation state was not retained")
			}
			f.journal.Event("admin-delete-failure-readback", map[string]any{"table": table, "all_owned_rows_unchanged": true, "inactive_retained": inactive, "credential_not_in_error": true})
		})
	}
}

func TestAfterFFAdminDeleteUserFencesLateProviderSQL(t *testing.T) {
	f := afterFFDeleteUserFixture(t)
	if err := f.service.UpdateUserActivateStatus(f.targetEmail, false); err != nil {
		t.Fatal(err)
	}
	paused, release := make(chan struct{}), make(chan struct{})
	var once, unblock sync.Once
	resume := func() { unblock.Do(func() { close(release) }) }
	var workers sync.WaitGroup
	callback := f.prefix + "_pause"
	if err := dao.DB.Callback().Delete().Before("gorm:delete").Register(callback, func(tx *gorm.DB) {
		if tx.Statement.Table == "tenant_model_instance" {
			once.Do(func() { close(paused); <-release })
		}
	}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { resume(); workers.Wait(); _ = dao.DB.Callback().Delete().Remove(callback) })
	deleted := make(chan error, 1)
	workers.Add(1)
	go func() { defer workers.Done(); _, err := f.service.DeleteUser(f.targetEmail); deleted <- err }()
	select {
	case <-paused:
	case <-time.After(10 * time.Second):
		t.Fatal("modern deletion pause not reached")
	}
	type providerResult struct {
		code common.ErrorCode
		err  error
	}
	created := make(chan providerResult, 1)
	workers.Add(1)
	go func() {
		defer workers.Done()
		code, err := service.NewModelProviderService().AddModelProvider("OpenAI", f.target)
		created <- providerResult{code, err}
	}()
	waiting := false
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		select {
		case result := <-created:
			t.Fatal("provider returned before tenant deletion completed", result.code)
		default:
		}
		var count int
		if err := f.read.QueryRow("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%t_ai_tenants%' AND query LIKE '%FOR UPDATE%'").Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count > 0 {
			waiting = true
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if !waiting {
		t.Fatal("late provider did not wait for Tenant lock")
	}
	f.journal.Event("admin-delete-late-provider-wait", map[string]any{"tenant_id": f.target, "waiting": waiting})
	resume()
	select {
	case err := <-deleted:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("user deletion did not drain")
	}
	select {
	case result := <-created:
		if result.code != common.CodeNotFound || result.err == nil {
			t.Fatal("late provider was not rejected", result.code, result.err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("late provider did not drain")
	}
	f.assertRetired()
	var orphans int
	if err := f.read.QueryRow("SELECT count(*) FROM tenant_model_provider WHERE tenant_id=$1", f.target).Scan(&orphans); err != nil || orphans != 0 {
		t.Fatal("late provider orphan remains", orphans)
	}
	f.journal.Event("admin-delete-late-provider-readback", map[string]any{"orphan_providers": orphans, "late_create_rejected": true})
}
