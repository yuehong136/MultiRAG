package handler

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http/httptest"
	"os"
	"reflect"
	"strconv"
	"strings"
	"testing"

	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/service"
	"multirag/internal/testsupport"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	gormlog "gorm.io/gorm/logger"
)

type documentStopHTTPFixture struct {
	t                                               *testing.T
	db                                              *gorm.DB
	read                                            *sql.DB
	redis                                           *redis.Client
	journal                                         *testsupport.CaseJournal
	owner, kb, otherKB, a, b, foreign, taskA, taskB string
}

func newDocumentStopHTTPFixture(t *testing.T) *documentStopHTTPFixture {
	t.Helper()
	path := os.Getenv("MULTIRAG_AFTERFF_G_CONFIG")
	if path == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned SQL/Redis config required")
		}
		t.Skip("explicit owned SQL/Redis config required")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("private config unavailable")
	}
	var cfg struct {
		Database server.DatabaseConfig
		Redis    server.RedisConfig
	}
	if json.Unmarshal(raw, &cfg) != nil {
		t.Fatal("invalid private config")
	}
	if host, port, err := net.SplitHostPort(cfg.Redis.Host); err == nil {
		value, err := strconv.Atoi(port)
		if err != nil || value != cfg.Redis.Port {
			t.Fatal("inconsistent Redis endpoint")
		}
		cfg.Redis.Host = host
	}
	if cfg.Database.Host != "127.0.0.1" || cfg.Database.Port != 5432 || !strings.HasPrefix(cfg.Database.Database, "multirag_batch_g_") || cfg.Database.Schema != "usr_ai" || cfg.Redis.Host != "127.0.0.1" || cfg.Redis.Port != 6379 || cfg.Redis.DB != 1 {
		t.Fatal("unowned SQL/Redis endpoint")
	}
	journal := testsupport.NewCase(t)
	journal.Event("borrow-services", map[string]any{"database": cfg.Database.Database, "sql_host": cfg.Database.Host, "sql_port": cfg.Database.Port, "schema": cfg.Database.Schema, "redis_host": cfg.Redis.Host, "redis_port": cfg.Redis.Port, "redis_db": cfg.Redis.DB, "http_layer": "Gin ServeHTTP with controlled principal middleware; no network listener"})
	dsn, err := dao.PostgresDSN(cfg.Database)
	if err != nil {
		t.Fatal("invalid owned SQL config")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: gormlog.Default.LogMode(gormlog.Silent)})
	if err != nil {
		t.Fatal("owned SQL unavailable")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	read, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("independent SQL unavailable")
	}
	t.Cleanup(func() { read.Close() })
	client := redis.NewClient(&redis.Options{Addr: fmt.Sprintf("%s:%d", cfg.Redis.Host, cfg.Redis.Port), DB: cfg.Redis.DB, Password: cfg.Redis.Password})
	t.Cleanup(func() { client.Close() })
	if client.Ping(context.Background()).Err() != nil {
		t.Fatal("owned Redis unavailable")
	}
	journal.Event("reserve-schema", map[string]any{"database": cfg.Database.Database, "tables": []string{"t_ai_knowledgebases", "t_ai_documents", "t_ai_tasks", "t_ai_user_tenants"}})
	if db.AutoMigrate(&entity.Knowledgebase{}, &entity.Document{}, &entity.Task{}, &entity.UserTenant{}) != nil {
		t.Fatal("owned schema unavailable")
	}
	prefix := "gstop" + strings.ReplaceAll(uuid.NewString(), "-", "")[:17]
	f := &documentStopHTTPFixture{t: t, db: db, read: read, redis: client, journal: journal, owner: prefix + "owner", kb: prefix + "kb", otherKB: prefix + "other", a: prefix + "a", b: prefix + "b", foreign: prefix + "foreign", taskA: prefix + "ta", taskB: prefix + "tb"}
	ids := []string{f.a, f.b, f.foreign}
	tasks := []string{f.taskA, f.taskB}
	kbs := []string{f.kb, f.otherKB}
	keys := []string{f.taskA + "-cancel", f.taskB + "-cancel", "task-runtime:v1:" + f.taskA, "task-runtime:v1:" + f.taskB}
	journal.Event("reserve-resources", map[string]any{"sql_document_ids": ids, "sql_task_ids": tasks, "sql_dataset_ids": kbs, "principal_ids": []string{f.owner, prefix + "stranger"}, "redis_keys": keys})
	t.Cleanup(func() {
		if err := db.Transaction(func(tx *gorm.DB) error {
			if err := tx.Where("id IN ?", tasks).Delete(&entity.Task{}).Error; err != nil {
				return err
			}
			if err := tx.Where("id IN ?", ids).Delete(&entity.Document{}).Error; err != nil {
				return err
			}
			return tx.Where("id IN ?", kbs).Delete(&entity.Knowledgebase{}).Error
		}); err != nil {
			t.Error("owned SQL cleanup failed")
		}
		if err := client.Del(context.Background(), keys...).Err(); err != nil {
			t.Error("owned Redis cleanup failed")
		}
		var count int
		if err := read.QueryRow("SELECT (SELECT count(*) FROM t_ai_documents WHERE id IN ($1,$2,$3))+(SELECT count(*) FROM t_ai_tasks WHERE id IN ($4,$5))+(SELECT count(*) FROM t_ai_knowledgebases WHERE id IN ($6,$7))", f.a, f.b, f.foreign, f.taskA, f.taskB, f.kb, f.otherKB).Scan(&count); err != nil || count != 0 {
			t.Error("SQL cleanup readback", count, err)
		}
		remaining, err := client.Exists(context.Background(), keys...).Result()
		if err != nil || remaining != 0 {
			t.Error("Redis cleanup readback", remaining, err)
		}
		journal.Event("cleanup-readback", map[string]any{"sql_remaining": count, "redis_remaining": remaining, "borrowed_services_preserved": true})
	})
	one := func(v string) *string { return &v }
	kbsRows := []entity.Knowledgebase{{ID: f.kb, TenantID: f.owner, CreatedBy: f.owner, Name: prefix, EmbdID: "", Permission: "team", Status: one("1"), ParserConfig: entity.JSONMap{}}, {ID: f.otherKB, TenantID: f.owner, CreatedBy: f.owner, Name: prefix + "other", EmbdID: "", Permission: "me", Status: one("1"), ParserConfig: entity.JSONMap{}}}
	docs := []entity.Document{{ID: f.a, KbID: f.kb, CreatedBy: f.owner, Name: one("a"), ParserID: "naive", ParserConfig: entity.JSONMap{}, Run: one("1"), Status: one("1")}, {ID: f.b, KbID: f.kb, CreatedBy: f.owner, Name: one("b"), ParserID: "naive", ParserConfig: entity.JSONMap{}, Run: one("1"), Status: one("1")}, {ID: f.foreign, KbID: f.otherKB, CreatedBy: f.owner, Name: one("foreign"), ParserID: "naive", ParserConfig: entity.JSONMap{}, Run: one("1"), Status: one("1")}}
	for _, rows := range []any{&kbsRows, &docs, &[]entity.Task{{ID: f.taskA, DocID: f.a, Progress: 0.2}, {ID: f.taskB, DocID: f.b, Progress: 0.6}}} {
		if err := db.Create(rows).Error; err != nil {
			t.Fatal("owned SQL seeding failed")
		}
	}
	return f
}

func (f *documentStopHTTPFixture) snapshot() string {
	f.t.Helper()
	var raw string
	if err := f.read.QueryRow("SELECT json_build_object('docs',(SELECT json_agg(to_jsonb(d) ORDER BY id) FROM t_ai_documents d WHERE id IN ($1,$2,$3)),'tasks',(SELECT json_agg(to_jsonb(t) ORDER BY id) FROM t_ai_tasks t WHERE id IN ($4,$5)))::text", f.a, f.b, f.foreign, f.taskA, f.taskB).Scan(&raw); err != nil {
		f.t.Fatal("independent SQL snapshot unavailable")
	}
	return raw
}

func TestDocumentStopHTTPRealSQLRedis(t *testing.T) {
	f := newDocumentStopHTTPFixture(t)
	documents := &service.DocumentService{}
	documents.SetDocumentStopService(service.NewDocumentStopService(f.db, f.redis))
	h := NewDocumentHandler(documents)
	router := gin.New()
	// Only request identity is controlled. Handler authorization, transaction,
	// cancellation writes and independent SQL/Redis readbacks are real.
	router.Use(func(c *gin.Context) {
		if id := c.GetHeader("X-Test-Principal"); id != "" {
			c.Set("user", &entity.User{ID: id})
		}
	})
	router.POST("/api/v1/datasets/:dataset_id/documents/stop", h.StopParseDocuments)
	type response struct {
		Code common.ErrorCode
		Data struct {
			SuccessCount int             `json:"success_count"`
			Errors       json.RawMessage `json:"errors"`
		}
	}
	request := func(t *testing.T, principal, body string) response {
		t.Helper()
		w := httptest.NewRecorder()
		req := httptest.NewRequest("POST", "/api/v1/datasets/"+f.kb+"/documents/stop", strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Test-Principal", principal)
		router.ServeHTTP(w, req)
		var result response
		if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &result) != nil {
			t.Fatalf("invalid HTTP envelope: %s", w.Body.String())
		}
		return result
	}
	body := fmt.Sprintf(`{"document_ids":[%q,%q,%q]}`, f.b, f.a, f.a)
	before := f.snapshot()
	for _, tc := range []struct {
		name, principal, body string
		code                  common.ErrorCode
	}{
		{"missing_auth", "", body, common.CodeUnauthorized},
		{"no_membership", f.owner + "x", body, common.CodeForbidden},
		{"mixed_foreign", f.owner, fmt.Sprintf(`{"document_ids":[%q,%q]}`, f.a, f.foreign), common.CodeForbidden},
		{"null", f.owner, `{"document_ids":null}`, common.CodeBadRequest},
		{"wrong_element", f.owner, `{"document_ids":[3]}`, common.CodeBadRequest},
		{"duplicate_key", f.owner, `{"document_ids":["a"],"document_ids":["b"]}`, common.CodeBadRequest},
	} {
		t.Run(tc.name, func(t *testing.T) {
			result := request(t, tc.principal, tc.body)
			if result.Code != tc.code || f.snapshot() != before {
				t.Fatalf("request changed SQL or returned %d; wanted %d", result.Code, tc.code)
			}
			n, err := f.redis.Exists(context.Background(), f.taskA+"-cancel", f.taskB+"-cancel").Result()
			if err != nil || n != 0 {
				t.Fatal("rejection wrote cancellation fences", n, err)
			}
		})
	}
	result := request(t, f.owner, body)
	if result.Code != common.CodeSuccess || result.Data.SuccessCount != 2 || len(result.Data.Errors) != 0 {
		t.Fatalf("incomplete document acknowledgement: %+v", result)
	}
	var canceled int
	if err := f.read.QueryRow("SELECT count(*) FROM t_ai_documents d JOIN t_ai_tasks t ON t.doc_id=d.id WHERE d.id IN ($1,$2) AND d.run='2' AND d.progress=0 AND t.progress=-1", f.a, f.b).Scan(&canceled); err != nil || canceled != 2 {
		t.Fatal("cancellation not committed", canceled, err)
	}
	nonces, err := f.redis.MGet(context.Background(), f.taskA+"-cancel", f.taskB+"-cancel").Result()
	if err != nil || len(nonces) != 2 || nonces[0] == nil || nonces[1] == nil || nonces[0] == nonces[1] {
		t.Fatal("distinct cancellation nonces missing", err)
	}
	committed := f.snapshot()
	result = request(t, f.owner, body)
	after, err := f.redis.MGet(context.Background(), f.taskA+"-cancel", f.taskB+"-cancel").Result()
	if err != nil || result.Code != common.CodeSuccess || result.Data.SuccessCount != 2 || f.snapshot() != committed || !reflect.DeepEqual(nonces, after) {
		t.Fatal("repeat cancellation was not idempotent", err)
	}
	f.journal.Event("http-business-readback", map[string]any{"distinct_acknowledged": 2, "sql_canceled": canceled, "denied_requests_have_zero_effects": true, "repeat_preserved_nonces": true, "worker_termination_proven": false})
}

func TestDocumentGetByIDsLiveProjectionAndContext(t *testing.T) {
	f := newDocumentStopHTTPFixture(t)
	previous := dao.DB
	dao.DB = f.db
	t.Cleanup(func() { dao.DB = previous })
	if err := f.db.Model(&entity.Document{}).Where("id = ?", f.b).Update("status", "0").Error; err != nil {
		t.Fatal(err)
	}
	documents, err := dao.NewDocumentDAO().GetByIDs(context.Background(), []string{f.a, f.b, f.a, f.owner + "missing"})
	if err != nil || len(documents) != 2 {
		t.Fatal("duplicate/missing/disabled DAO contract", len(documents), err)
	}
	seen := map[string]bool{}
	for _, doc := range documents {
		if doc.KbID != f.kb || doc.Name != nil || doc.CreatedBy != "" || doc.Status != nil || doc.ParserConfig != nil {
			t.Fatal("DAO loaded fields beyond parent ID/dataset projection")
		}
		seen[doc.ID] = true
	}
	if !seen[f.a] || !seen[f.b] {
		t.Fatal("disabled parent was omitted")
	}
	missing, err := dao.NewDocumentDAO().GetByIDs(context.Background(), []string{f.owner + "missing"})
	if err != nil || len(missing) != 0 {
		t.Fatal("no-match query failed", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := dao.NewDocumentDAO().GetByIDs(ctx, []string{f.a}); !errors.Is(err, context.Canceled) {
		t.Fatal("DAO lost request cancellation", err)
	}
	f.journal.Event("dao-business-readback", map[string]any{"selected": len(documents), "duplicates_collapsed": true, "disabled_parent_included": true, "projection": "id,kb_id", "request_context_preserved": true})
}
