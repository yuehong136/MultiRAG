package service

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"testing"

	"github.com/google/uuid"
	"gorm.io/gorm"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/testsupport"
)

// TestAfterFFMemoryModelBindingsSQL exercises the actual memory consumer against
// owned SQL. Serial model IDs are registered by their unique natural key before
// INSERT, then their assigned positive ID is recorded and independently read.
func TestAfterFFMemoryModelBindingsSQL(t *testing.T) {
	journal := testsupport.NewCase(t)
	config := os.Getenv("MULTIRAG_AFTERFF_G_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned memory model SQL config required")
		}
		t.Skip("requires owned afterff G PostgreSQL config")
	}
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal("owned SQL configuration unavailable")
	}
	cfg := server.GetConfig()
	if cfg.Database.Host != "127.0.0.1" || cfg.Database.Port != 5432 || cfg.Database.Database != "multirag_batch_g_afterff_20261028" || cfg.Database.Schema != "usr_ai" {
		t.Fatal("exact owned PostgreSQL target required")
	}
	journal.Event("reserve-schema-before-SQL", map[string]any{"host": cfg.Database.Host, "port": cfg.Database.Port, "database": cfg.Database.Database, "schema": cfg.Database.Schema})
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
	owner, foreign := "gfm-own-"+suffix, "gfm-oth-"+suffix
	callbackName := "afterff_memory_resources_" + suffix
	memoryWrites := 0
	if err := dao.DB.Callback().Create().Before("gorm:create").Register(callbackName, func(tx *gorm.DB) {
		if tx.Statement.Table == "t_ai_tenant_llms" {
			row, ok := tx.Statement.Dest.(*entity.TenantLLM)
			if !ok || row.TenantID == "" || row.LLMName == nil || *row.LLMName == "" {
				tx.AddError(errors.New("numeric binding natural key required before SQL"))
				return
			}
			journal.Event("reserve-numeric-model-natural-key-before-SQL", map[string]any{"table": tx.Statement.Table, "tenant_id": row.TenantID, "llm_factory": row.LLMFactory, "llm_name": *row.LLMName, "numeric_id": "database assigned"})
			return
		}
		value := reflect.Indirect(tx.Statement.ReflectValue)
		if value.Kind() != reflect.Struct {
			tx.AddError(errors.New("explicit fixture identity required before SQL"))
			return
		}
		id := value.FieldByName("ID")
		if !id.IsValid() || id.Kind() != reflect.String || id.String() == "" {
			tx.AddError(errors.New("explicit fixture ID required before SQL"))
			return
		}
		journal.Event("reserve-row-before-SQL", map[string]any{"table": tx.Statement.Table, "id": id.String()})
		if tx.Statement.Table == "t_ai_memories" {
			memoryWrites++
		}
	}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = dao.DB.Callback().Create().Remove(callbackName) })
	// The unique tenant namespaces are reserved before creating any rows. They
	// include failed INSERT identities even if no serial ID was assigned.
	journal.Event("reserve-memory-fixture-namespaces", map[string]any{"tenant_ids": []string{owner, foreign}, "tables": []string{"t_ai_tenants", "t_ai_tenant_llms", "t_ai_memories"}})
	t.Cleanup(func() {
		for _, entry := range []struct{ table, column string }{{"t_ai_memories", "tenant_id"}, {"t_ai_tenant_llms", "tenant_id"}, {"t_ai_tenants", "id"}} {
			if _, err := read.Exec("DELETE FROM "+entry.table+" WHERE "+entry.column+" IN ($1,$2)", owner, foreign); err != nil {
				t.Error("owned memory fixture cleanup failed")
			}
			var remaining int
			err := read.QueryRow("SELECT count(*) FROM "+entry.table+" WHERE "+entry.column+" IN ($1,$2)", owner, foreign).Scan(&remaining)
			journal.Event("cleanup-memory-model-SQL-readback", map[string]any{"table": entry.table, "tenant_ids": []string{owner, foreign}, "remaining": remaining, "confirmed": err == nil && remaining == 0})
			if err != nil || remaining != 0 {
				t.Error("owned memory fixture rows remain")
			}
		}
	})
	mustCreate := func(value any) {
		t.Helper()
		if err := dao.DB.Create(value).Error; err != nil {
			state := "unavailable"
			var sqlState interface{ SQLState() string }
			if errors.As(err, &sqlState) {
				state = sqlState.SQLState()
			}
			t.Fatalf("fixture create failed: row_type=%T error_type=%T SQLSTATE=%s", value, err, state)
		}
	}
	active := "1"
	mustCreate(&entity.Tenant{ID: owner, Status: &active})
	mustCreate(&entity.Tenant{ID: foreign, Status: &active})
	modelRow := func(tenant, name, factory, kind, status string) *entity.TenantLLM {
		t.Helper()
		row := &entity.TenantLLM{TenantID: tenant, LLMFactory: factory, LLMName: &name, ModelType: &kind, Status: status, MaxTokens: 123}
		mustCreate(row)
		if row.ID <= 0 {
			t.Fatal("SQL did not assign a positive numeric model ID")
		}
		var readID int64
		if err := read.QueryRow("SELECT id FROM t_ai_tenant_llms WHERE tenant_id=$1 AND llm_factory=$2 AND llm_name=$3", tenant, factory, name).Scan(&readID); err != nil || readID != row.ID {
			t.Fatal("independent numeric ID readback failed")
		}
		journal.Event("numeric-model-assigned-ID-readback", map[string]any{"tenant_id": tenant, "llm_factory": factory, "llm_name": name, "id": readID})
		return row
	}
	// This retained inventory intentionally has no modern catalog entry.
	factory := "ExistingFactory"
	chat := modelRow(owner, "memory-chat", factory, "chat", "1")
	embed := modelRow(owner, "memory-embed", factory, "embedding", "1")
	foreignChat := modelRow(foreign, "foreign-chat", factory, "chat", "1")
	foreignEmbed := modelRow(foreign, "foreign-embed", factory, "embedding", "1")
	inactiveChat := modelRow(owner, "inactive-chat", factory, "chat", "0")
	inactiveEmbed := modelRow(owner, "inactive-embed", factory, "embedding", "0")
	modelRow(owner, "ambiguous-chat", "ExistingA", "chat", "1")
	modelRow(owner, "ambiguous-chat", "ExistingB", "chat", "1")
	nullTypeName := "missing-type"
	missingType := &entity.TenantLLM{TenantID: owner, LLMFactory: factory, LLMName: &nullTypeName, Status: "1", MaxTokens: 123}
	mustCreate(missingType)
	chatSelector, embedSelector := "memory-chat@"+factory, "memory-embed@"+factory
	idText := func(value int64) string { return strconv.FormatInt(value, 10) }
	pointer := func(value string) *string { return &value }
	request := func(label string) *CreateMemoryRequest {
		return &CreateMemoryRequest{Name: "gfm-" + label + "-" + suffix, MemoryType: []string{"raw"}, LLMID: chatSelector, EmbdID: embedSelector}
	}
	snapshot := func() string {
		t.Helper()
		var rows []string
		for _, table := range []string{"t_ai_memories", "t_ai_tenant_llms", "t_ai_tenants"} {
			var raw string
			if err := read.QueryRow("SELECT COALESCE(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text)::text,'[]') FROM " + table + " x").Scan(&raw); err != nil {
				t.Fatal("memory SQL snapshot failed")
			}
			rows = append(rows, raw)
		}
		return fmt.Sprintf("%x", sha256.Sum256([]byte(strings.Join(rows, "\n"))))
	}
	memories := NewMemoryService()
	for _, test := range []struct {
		name   string
		change func(*CreateMemoryRequest)
	}{
		{"missing-chat", func(r *CreateMemoryRequest) { r.LLMID = "" }},
		{"missing-embed", func(r *CreateMemoryRequest) { r.EmbdID = "" }},
		{"unresolved-chat", func(r *CreateMemoryRequest) { r.LLMID = "absent@" + factory }},
		{"unresolved-instance", func(r *CreateMemoryRequest) { r.LLMID = "absent@instance@OpenAI" }},
		{"foreign-chat-name", func(r *CreateMemoryRequest) { r.LLMID = "foreign-chat@" + factory }},
		{"foreign-embed-name", func(r *CreateMemoryRequest) { r.EmbdID = "foreign-embed@" + factory }},
		{"foreign-chat-id", func(r *CreateMemoryRequest) { r.LLMID = idText(foreignChat.ID) }},
		{"foreign-embed-id", func(r *CreateMemoryRequest) { r.EmbdID = idText(foreignEmbed.ID) }},
		{"foreign-explicit-id", func(r *CreateMemoryRequest) { r.TenantLLMID = pointer(idText(foreignChat.ID)) }},
		{"inactive-chat-name", func(r *CreateMemoryRequest) { r.LLMID = "inactive-chat@" + factory }},
		{"inactive-embed-name", func(r *CreateMemoryRequest) { r.EmbdID = "inactive-embed@" + factory }},
		{"inactive-chat-id", func(r *CreateMemoryRequest) { r.LLMID = idText(inactiveChat.ID) }},
		{"inactive-embed-id", func(r *CreateMemoryRequest) { r.EmbdID = idText(inactiveEmbed.ID) }},
		{"wrong-chat-type", func(r *CreateMemoryRequest) { r.LLMID = "memory-embed@" + factory }},
		{"wrong-embed-type", func(r *CreateMemoryRequest) { r.EmbdID = "memory-chat@" + factory }},
		{"wrong-explicit-type", func(r *CreateMemoryRequest) { r.TenantLLMID = pointer(idText(embed.ID)) }},
		{"mismatched-explicit-name", func(r *CreateMemoryRequest) {
			r.LLMID = "foreign-chat@" + factory
			r.TenantLLMID = pointer(idText(chat.ID))
		}},
		{"mismatched-explicit-id", func(r *CreateMemoryRequest) { r.LLMID = idText(chat.ID); r.TenantLLMID = pointer(idText(embed.ID)) }},
		{"missing-row-id", func(r *CreateMemoryRequest) { r.LLMID = "9223372036854775807" }},
		{"zero-id", func(r *CreateMemoryRequest) { r.LLMID = "0" }},
		{"negative-id", func(r *CreateMemoryRequest) { r.LLMID = "-1" }},
		{"overflow-id", func(r *CreateMemoryRequest) { r.LLMID = "9223372036854775808" }},
		{"zero-explicit-id", func(r *CreateMemoryRequest) { r.TenantLLMID = pointer("0") }},
		{"bad-explicit-id", func(r *CreateMemoryRequest) { r.TenantLLMID = pointer("not-numeric") }},
		{"blank-explicit-id", func(r *CreateMemoryRequest) { r.TenantEmbdID = pointer("") }},
		{"spaced-explicit-id", func(r *CreateMemoryRequest) { r.TenantLLMID = pointer(" " + idText(chat.ID)) }},
		{"null-row-type", func(r *CreateMemoryRequest) { r.LLMID = idText(missingType.ID) }},
		{"ambiguous-bare-name", func(r *CreateMemoryRequest) { r.LLMID = "ambiguous-chat" }},
	} {
		t.Run(test.name, func(t *testing.T) {
			req := request(test.name)
			test.change(req)
			before := snapshot()
			writes := memoryWrites
			requestBefore, _ := json.Marshal(req)
			result, err := memories.CreateMemory(owner, req)
			requestAfter, _ := json.Marshal(req)
			if err == nil || result != nil || snapshot() != before || memoryWrites != writes || string(requestBefore) != string(requestAfter) {
				t.Fatal("invalid memory binding reached INSERT, changed request, or modified SQL")
			}
		})
	}
	for _, test := range []struct {
		name string
		req  *CreateMemoryRequest
	}{
		{"canonical", request("canonical")},
		{"numeric", func() *CreateMemoryRequest {
			r := request("numeric")
			r.LLMID = idText(chat.ID)
			r.EmbdID = idText(embed.ID)
			return r
		}()},
		{"explicit", func() *CreateMemoryRequest {
			r := request("explicit")
			r.TenantLLMID = pointer(idText(chat.ID))
			r.TenantEmbdID = pointer(idText(embed.ID))
			return r
		}()},
		{"bare", func() *CreateMemoryRequest {
			r := request("bare")
			r.LLMID = "memory-chat"
			r.EmbdID = "memory-embed"
			return r
		}()},
	} {
		t.Run(test.name, func(t *testing.T) {
			reqBefore, _ := json.Marshal(test.req)
			result, err := memories.CreateMemory(owner, test.req)
			reqAfter, _ := json.Marshal(test.req)
			if err != nil || result == nil || string(reqBefore) != string(reqAfter) {
				t.Fatal("owned active model binding no longer creates memory")
			}
			var storedChat, storedEmbed int64
			var storedTenant, storedLLM, storedEmbd string
			if err := read.QueryRow("SELECT tenant_id,tenant_llm_id,tenant_embd_id,llm_id,embd_id FROM t_ai_memories WHERE id=$1", result.ID).Scan(&storedTenant, &storedChat, &storedEmbed, &storedLLM, &storedEmbd); err != nil || storedTenant != owner || storedChat != chat.ID || storedEmbed != embed.ID || storedLLM != test.req.LLMID || storedEmbd != test.req.EmbdID || storedChat <= 0 || storedEmbed <= 0 {
				t.Fatal("memory persisted zero, wrong-type, or cross-tenant model bindings")
			}
		})
	}
	before := snapshot()
	writes := memoryWrites
	if result, err := memories.CreateMemory(owner, nil); err == nil || result != nil || snapshot() != before || memoryWrites != writes {
		t.Fatal("nil creation request reached SQL INSERT")
	}
	journal.Event("memory-model-binding-contract-readback", map[string]any{"tenant_id": owner, "chat_id": chat.ID, "embedding_id": embed.ID, "retained_factory": factory, "successful_memories": 4, "denied_before_memory_INSERT": 28, "positive_numeric_bindings": true, "provider_IO": false, "update_memory_scope": "not changed"})
}
