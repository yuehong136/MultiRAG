package admin

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"sync/atomic"
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

type afterFFNumericOutboundTrap struct{ calls atomic.Int64 }

func (trap *afterFFNumericOutboundTrap) RoundTrip(*http.Request) (*http.Response, error) {
	trap.calls.Add(1)
	return nil, errors.New("model configuration must not contact a provider")
}

// These are active SQL consumers of numeric TenantLLM IDs, even though model
// discovery and provider management use the newer configured-instance tables.
func TestAfterFFAdminNumericModelConsumersSQL(t *testing.T) {
	config := os.Getenv("MULTIRAG_AFTERFF_G_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned afterff G PostgreSQL configuration required")
		}
		t.Skip("requires owned afterff G PostgreSQL configuration")
	}
	j := testsupport.NewCase(t)
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
	j.Event("reserve-numeric-model-schema-before-SQL", map[string]any{"host": cfg.Database.Host, "port": cfg.Database.Port, "database": cfg.Database.Database, "schema": cfg.Database.Schema})
	if err := dao.InitDB(); err != nil {
		t.Fatal("owned schema initialization failed")
	}
	pool, err := dao.DB.DB()
	if err != nil {
		t.Fatal("owned SQL pool unavailable")
	}
	t.Cleanup(func() { pool.Close() })
	dsn, err := dao.PostgresDSN(cfg.Database)
	if err != nil {
		t.Fatal("owned SQL DSN unavailable")
	}
	reader, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("independent SQL reader unavailable")
	}
	t.Cleanup(func() { reader.Close() })
	if reader.PingContext(context.Background()) != nil {
		t.Fatal("independent SQL endpoint unavailable")
	}
	trap := &afterFFNumericOutboundTrap{}
	previousTransport := http.DefaultTransport
	http.DefaultTransport = trap
	t.Cleanup(func() { http.DefaultTransport = previousTransport })
	previousDefaults := cfg.UserDefaultLLM.DefaultModels
	cfg.UserDefaultLLM.DefaultModels = server.DefaultModelsConfig{
		ChatModel:      server.ModelConfig{Factory: "OpenAI", Name: "gpt-4o-mini", APIKey: "owned-numeric-chat-secret", BaseURL: "https://owned-chat.invalid/v1"},
		EmbeddingModel: server.ModelConfig{Factory: "OpenAI", Name: "text-embedding-3-small", APIKey: "owned-numeric-embed-secret", BaseURL: "https://owned-embed.invalid/v1"},
		ASRModel:       server.ModelConfig{Factory: "Groq", Name: "whisper-large-v3", APIKey: "owned-numeric-asr-secret", BaseURL: "https://owned-asr.invalid/v1"},
	}
	t.Cleanup(func() { cfg.UserDefaultLLM.DefaultModels = previousDefaults })
	suffix := os.Getenv("MULTIRAG_RUN_ID")
	if len(suffix) < 12 {
		suffix = strings.ReplaceAll(uuid.NewString(), "-", "")
	}
	suffix = suffix[:12]
	callbackName := "afterff_numeric_models_" + suffix
	rowIDs := map[string][]string{}
	numericTenants := map[string]bool{}
	if err := dao.DB.Callback().Create().Before("gorm:create").Register(callbackName, func(tx *gorm.DB) {
		if tx.Statement.Table == "t_ai_tenant_llms" {
			row, ok := tx.Statement.Dest.(*entity.TenantLLM)
			if !ok || row.TenantID == "" || row.LLMName == nil {
				tx.AddError(errors.New("numeric natural key missing before SQL"))
				return
			}
			j.Event("reserve-numeric-binding-natural-key", map[string]any{"table": tx.Statement.Table, "tenant_id": row.TenantID, "llm_factory": row.LLMFactory, "llm_name": *row.LLMName, "numeric_id": "database assigned"})
			numericTenants[row.TenantID] = true
			return
		}
		value := reflect.Indirect(tx.Statement.ReflectValue)
		if value.Kind() != reflect.Struct {
			tx.AddError(errors.New("explicit row identity required before SQL"))
			return
		}
		id := value.FieldByName("ID")
		if !id.IsValid() || id.Kind() != reflect.String || id.String() == "" {
			tx.AddError(errors.New("explicit row ID required before SQL"))
			return
		}
		j.Event("reserve-numeric-consumer-row-before-SQL", map[string]any{"table": tx.Statement.Table, "id": id.String()})
		rowIDs[tx.Statement.Table] = append(rowIDs[tx.Statement.Table], id.String())
	}); err != nil {
		t.Fatal("resource callback registration failed")
	}
	t.Cleanup(func() {
		if err := dao.DB.Callback().Create().Remove(callbackName); err != nil {
			t.Error("resource callback removal failed")
		}
	})
	t.Cleanup(func() {
		for _, table := range []string{"t_ai_knowledgebases", "t_ai_memories", "tenant_model", "tenant_model_instance", "tenant_model_provider", "t_ai_tenant_llms", "t_ai_files", "t_ai_user_tenants", "t_ai_tenants", "t_ai_users"} {
			if table == "t_ai_tenant_llms" {
				for tenantID := range numericTenants {
					if _, err := reader.Exec("DELETE FROM t_ai_tenant_llms WHERE tenant_id=$1", tenantID); err != nil {
						t.Error("numeric binding cleanup failed")
					}
					var count int
					err := reader.QueryRow("SELECT count(*) FROM t_ai_tenant_llms WHERE tenant_id=$1", tenantID).Scan(&count)
					j.Event("numeric-binding-cleanup-readback", map[string]any{"tenant_id": tenantID, "remaining": count, "confirmed": err == nil && count == 0})
					if err != nil || count != 0 {
						t.Error("numeric binding cleanup readback failed")
					}
				}
				continue
			}
			ids := rowIDs[table]
			if len(ids) == 0 {
				continue
			}
			marks, args := make([]string, len(ids)), make([]any, len(ids))
			for i, id := range ids {
				marks[i], args[i] = fmt.Sprintf("$%d", i+1), id
			}
			condition := "id IN (" + strings.Join(marks, ",") + ")"
			if _, err := reader.Exec("DELETE FROM "+table+" WHERE "+condition, args...); err != nil {
				t.Error("numeric consumer row cleanup failed", table)
			}
			var count int
			err := reader.QueryRow("SELECT count(*) FROM "+table+" WHERE "+condition, args...).Scan(&count)
			j.Event("numeric-consumer-cleanup-readback", map[string]any{"table": table, "ids": ids, "remaining": count, "confirmed": err == nil && count == 0})
			if err != nil || count != 0 {
				t.Error("numeric consumer cleanup readback failed", table)
			}
		}
	})
	create := func(label string) string {
		t.Helper()
		result, err := NewService().CreateUser("gff-numeric-"+label+"-"+suffix+"@example.invalid", "owned-password!", "user")
		if err != nil || result == nil {
			t.Fatal("admin numeric account creation failed")
		}
		id, ok := result["id"].(string)
		if !ok || id == "" {
			t.Fatal("admin account identity missing")
		}
		return id
	}
	ownerID := create("owner")
	chatSelector := "gpt-4o-mini@default-chat@OpenAI"
	embedSelector := "text-embedding-3-small@default-embedding@OpenAI"
	asrSelector := "whisper-large-v3@default-asr@Groq"
	var chatID, embedID, asrID int64
	if reader.QueryRow("SELECT tenant_llm_id,tenant_embd_id,tenant_asr_id FROM t_ai_tenants WHERE id=$1", ownerID).Scan(&chatID, &embedID, &asrID) != nil || chatID <= 0 || embedID <= 0 || asrID <= 0 || chatID == embedID {
		t.Fatal("admin default numeric sidecars missing")
	}
	for _, check := range []struct {
		id                             int64
		factory, name, kind, key, base string
		limit                          int64
	}{
		{chatID, "OpenAI", "gpt-4o-mini@default-chat", "chat", "owned-numeric-chat-secret", "https://owned-chat.invalid/v1", 128000},
		{embedID, "OpenAI", "text-embedding-3-small@default-embedding", "embedding", "owned-numeric-embed-secret", "https://owned-embed.invalid/v1", 8191},
		{asrID, "Groq", "whisper-large-v3@default-asr", "speech2text", "owned-numeric-asr-secret", "https://owned-asr.invalid/v1", 0},
	} {
		var tenant, factory, name, kind, key, base, status string
		var maxTokens int64
		if reader.QueryRow("SELECT tenant_id,llm_factory,llm_name,mdl_type,api_key,api_base,max_tokens,status FROM t_ai_tenant_llms WHERE id=$1", check.id).Scan(&tenant, &factory, &name, &kind, &key, &base, &maxTokens, &status) != nil || tenant != ownerID || factory != check.factory || name != check.name || kind != check.kind || key != check.key || base != check.base || maxTokens != check.limit || status != "1" {
			t.Fatal("configured numeric binding differs from actual instance configuration")
		}
		j.Event("numeric-binding-ID-independent-readback", map[string]any{"id": check.id, "tenant_id": ownerID, "factory": factory, "model": name, "kind": kind, "max_tokens": maxTokens})
	}
	var rawExtra string
	if reader.QueryRow("SELECT m.extra FROM tenant_model m JOIN tenant_model_instance i ON i.id=m.instance_id JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.tenant_id=$1 AND i.instance_name='default-chat' AND m.model_name='gpt-4o-mini'", ownerID).Scan(&rawExtra) != nil {
		t.Fatal("modern configured chat declaration missing")
	}
	var extra struct {
		MaxTokens  int      `json:"max_tokens"`
		ModelTypes []string `json:"model_types"`
	}
	if json.Unmarshal([]byte(rawExtra), &extra) != nil || extra.MaxTokens != 128000 || !slices.Contains(extra.ModelTypes, "chat") || !slices.Contains(extra.ModelTypes, "vision") {
		t.Fatal("numeric binding narrowed the modern full capability declaration")
	}
	users := service.NewUserService()
	if err := users.SetTenantInfo(ownerID, &service.SetTenantInfoRequest{LLMID: &chatSelector, EmbdID: &embedSelector, ASRID: &asrSelector}); err != nil {
		t.Fatal("actual tenant default consumer rejected configured selectors")
	}
	var storedChatID, storedEmbedID, storedASRID int64
	if reader.QueryRow("SELECT tenant_llm_id,tenant_embd_id,tenant_asr_id FROM t_ai_tenants WHERE id=$1", ownerID).Scan(&storedChatID, &storedEmbedID, &storedASRID) != nil || storedChatID != chatID || storedEmbedID != embedID || storedASRID != asrID {
		t.Fatal("actual tenant default consumer lost numeric binding")
	}
	dataset, code, err := service.NewDatasetsService().CreateDataset(&service.CreateDatasetRequest{Name: "numeric-default-" + suffix}, ownerID)
	if err != nil || code != common.CodeSuccess || dataset == nil {
		t.Fatal("actual dataset consumer rejected admin default configuration")
	}
	datasetID, ok := dataset["id"].(string)
	if !ok || datasetID == "" {
		t.Fatal("actual dataset consumer omitted its dataset identity")
	}
	var datasetEmbeddingSelector string
	var datasetTenantNumeric int64
	if reader.QueryRow("SELECT kb.embd_id,t.tenant_embd_id FROM t_ai_knowledgebases kb JOIN t_ai_tenants t ON t.id=kb.tenant_id WHERE kb.id=$1 AND kb.tenant_id=$2", datasetID, ownerID).Scan(&datasetEmbeddingSelector, &datasetTenantNumeric) != nil || datasetEmbeddingSelector != embedSelector || datasetTenantNumeric != embedID {
		t.Fatal("dataset default lost canonical selector or tenant numeric binding")
	}
	memories := service.NewMemoryService()
	memory, err := memories.CreateMemory(ownerID, &service.CreateMemoryRequest{Name: "configured-default-" + suffix, MemoryType: []string{"raw"}, LLMID: chatSelector, EmbdID: embedSelector})
	if err != nil || memory == nil {
		t.Fatal("actual memory consumer could not create configured memory")
	}
	if reader.QueryRow("SELECT tenant_llm_id,tenant_embd_id FROM t_ai_memories WHERE id=$1 AND tenant_id=$2", memory.ID, ownerID).Scan(&storedChatID, &storedEmbedID) != nil || storedChatID != chatID || storedEmbedID != embedID {
		t.Fatal("memory consumer persisted zero or unrelated numeric references")
	}
	foreignID := create("foreign")
	var foreignChatID int64
	if reader.QueryRow("SELECT tenant_llm_id FROM t_ai_tenants WHERE id=$1", foreignID).Scan(&foreignChatID) != nil || foreignChatID <= 0 || foreignChatID == chatID {
		t.Fatal("admin initialization reused another tenant's numeric binding")
	}
	afterFFCheckDefaultRetrieval(t, j, reader, ownerID, foreignID, datasetID, previousTransport)
	legacyName, legacyKind, legacyKey, legacyBase := "existing-chat", "chat", "owned-existing-secret", "https://existing.invalid/v1"
	legacy := entity.TenantLLM{TenantID: ownerID, LLMFactory: "ExistingFactory", LLMName: &legacyName, ModelType: &legacyKind, APIKey: &legacyKey, APIBase: &legacyBase, MaxTokens: 17, Status: "1"}
	if dao.DB.Create(&legacy).Error != nil || legacy.ID <= 0 {
		t.Fatal("existing numeric fixture creation failed")
	}
	legacySelector := legacyName + "@ExistingFactory"
	if err := users.SetTenantInfo(ownerID, &service.SetTenantInfoRequest{LLMID: &legacySelector}); err != nil {
		t.Fatal("existing numeric selector is no longer usable")
	}
	legacySnapshot := func() string {
		t.Helper()
		var row string
		if reader.QueryRow("SELECT to_jsonb(x)::text FROM t_ai_tenant_llms x WHERE id=$1", legacy.ID).Scan(&row) != nil {
			t.Fatal("existing numeric row readback failed")
		}
		return fmt.Sprintf("%x", sha256.Sum256([]byte(row)))
	}
	beforeLegacy := legacySnapshot()
	create("later")
	var retainedSelector string
	if reader.QueryRow("SELECT llm_id,tenant_llm_id FROM t_ai_tenants WHERE id=$1", ownerID).Scan(&retainedSelector, &storedChatID) != nil || retainedSelector != legacySelector || storedChatID != legacy.ID || legacySnapshot() != beforeLegacy {
		t.Fatal("admin initialization rewrote existing numeric rows or selectors")
	}
	legacyMemory, err := memories.CreateMemory(ownerID, &service.CreateMemoryRequest{Name: "existing-default-" + suffix, MemoryType: []string{"raw"}, LLMID: legacySelector, EmbdID: embedSelector})
	if err != nil || legacyMemory == nil || legacyMemory.TenantLLMID == nil || *legacyMemory.TenantLLMID != legacy.ID {
		t.Fatal("existing numeric selector no longer resolves in memory consumer")
	}
	foreignName, inactiveName := "foreign-only@legacy-instance", "inactive-only@legacy-instance"
	foreignRow := entity.TenantLLM{TenantID: foreignID, LLMFactory: "OpenAI", LLMName: &foreignName, ModelType: &legacyKind, APIKey: &legacyKey, APIBase: &legacyBase, MaxTokens: 19, Status: "1"}
	inactiveRow := entity.TenantLLM{TenantID: ownerID, LLMFactory: "OpenAI", LLMName: &inactiveName, ModelType: &legacyKind, APIKey: &legacyKey, APIBase: &legacyBase, MaxTokens: 23, Status: "0"}
	if dao.DB.Create(&foreignRow).Error != nil || dao.DB.Create(&inactiveRow).Error != nil {
		t.Fatal("foreign/inactive numeric fixture creation failed")
	}
	foreignSelector, inactiveSelector := foreignName+"@OpenAI", inactiveName+"@OpenAI"
	if row, err := service.NewTenantLLMService().GetAPIKey(ownerID, foreignSelector); err == nil || row != nil {
		t.Fatal("numeric lookup crossed tenant boundary")
	}
	var memoryCountBefore int
	if reader.QueryRow("SELECT count(*) FROM t_ai_memories WHERE tenant_id=$1", ownerID).Scan(&memoryCountBefore) != nil {
		t.Fatal("memory count readback failed")
	}
	for _, denied := range []string{foreignSelector, inactiveSelector} {
		if err := users.SetTenantInfo(ownerID, &service.SetTenantInfoRequest{LLMID: &denied}); err == nil {
			t.Fatal("tenant settings accepted unavailable numeric binding")
		}
		if result, err := memories.CreateMemory(ownerID, &service.CreateMemoryRequest{Name: "denied-default-" + suffix, MemoryType: []string{"raw"}, LLMID: denied, EmbdID: embedSelector}); err == nil || result != nil {
			t.Fatal("memory consumer accepted a cross-tenant or inactive numeric binding")
		}
	}
	if err := users.SetTenantInfo(ownerID, &service.SetTenantInfoRequest{TenantID: &foreignID, LLMID: &chatSelector}); err == nil {
		t.Fatal("tenant setting crossed tenant boundary")
	}
	var memoryCountAfter int
	if reader.QueryRow("SELECT count(*) FROM t_ai_memories WHERE tenant_id=$1", ownerID).Scan(&memoryCountAfter) != nil || memoryCountAfter != memoryCountBefore {
		t.Fatal("denied numeric consumers wrote memory rows")
	}
	if legacySnapshot() != beforeLegacy {
		t.Fatal("consumer validation rewrote existing numeric binding")
	}
	if trap.calls.Load() != 0 {
		t.Fatal("numeric initialization or SQL consumers called a model provider")
	}
	j.Event("admin-numeric-active-consumers-readback", map[string]any{"tenant_id": ownerID, "chat_numeric_id": chatID, "embedding_numeric_id": embedID, "asr_numeric_id": asrID, "asr_max_tokens": 0, "foreign_tenant_id": foreignID, "foreign_chat_numeric_id": foreignChatID, "existing_numeric_id": legacy.ID, "existing_row_unchanged": true, "modern_full_capabilities_retained": true, "dataset_id": datasetID, "dataset_embedding_selector": datasetEmbeddingSelector, "configured_memory_id": memory.ID, "legacy_memory_id": legacyMemory.ID, "denied_memory_writes": 0, "provider_calls": trap.calls.Load()})
}
