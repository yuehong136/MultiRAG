package service

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/storage"
	"multirag/internal/testsupport"

	"github.com/google/uuid"
	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	"github.com/redis/go-redis/v9"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	gormlog "gorm.io/gorm/logger"
)

// Only storage/index acknowledgements are controlled here. SQL row locks,
// transactions, Redis nonces/Lua and independent SQL/Redis readback are real.
type deleteLiveIndex struct {
	engine.DocEngine
	mu                         sync.Mutex
	datasets                   map[string]bool
	rows                       map[string]map[string]any
	metadata                   map[string]string
	failDelete                 bool
	expectedBase               map[string]string
	pauseEntered, pauseRelease chan struct{}
	pauseOnce                  sync.Once
	loseGraphUpdate            bool
}

func (s *deleteLiveIndex) ChunkStoreExists(ctx context.Context, base string, kb string) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if expected, ok := s.expectedBase[kb]; ok && expected != base {
		return false, errors.New("wrong backend index base")
	}
	return s.datasets[kb], nil
}
func (s *deleteLiveIndex) MetadataStoreExists(ctx context.Context, _ string) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, err
	}
	return true, nil
}
func deleteLiveMatches(value string, selection any) bool {
	switch values := selection.(type) {
	case string:
		return value == values
	case []string:
		for _, candidate := range values {
			if value == candidate {
				return true
			}
		}
	}
	return false
}
func (s *deleteLiveIndex) Search(ctx context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	rows := []map[string]any{}
	if len(req.IndexNames) == 1 && strings.HasPrefix(req.IndexNames[0], "multirag_doc_meta_") {
		for id, kb := range s.metadata {
			if len(req.KbIDs) == 1 && req.KbIDs[0] == kb && deleteLiveMatches(id, req.Filter["id"]) {
				rows = append(rows, map[string]any{"id": id, "kb_id": kb})
			}
		}
		return &types.SearchResult{Chunks: rows[:min(len(rows), req.Limit)], Total: int64(len(rows))}, nil
	}
	if len(req.KbIDs) == 1 {
		if expected, ok := s.expectedBase[req.KbIDs[0]]; ok && (len(req.IndexNames) != 1 || req.IndexNames[0] != expected) {
			return nil, errors.New("wrong search index base")
		}
	}
	for _, row := range s.rows {
		kb, _ := row["kb_id"].(string)
		if len(req.KbIDs) != 1 || req.KbIDs[0] != kb {
			continue
		}
		matches := true
		for key, value := range req.Filter {
			text, _ := row[key].(string)
			if !deleteLiveMatches(text, value) {
				matches = false
			}
		}
		if matches {
			copy := map[string]any{}
			for _, key := range req.SelectFields {
				if value, ok := row[key]; ok {
					copy[key] = value
				}
			}
			rows = append(rows, copy)
		}
	}
	sort.Slice(rows, func(i, j int) bool { return rows[i]["id"].(string) < rows[j]["id"].(string) })
	total := int64(len(rows))
	start := min(req.Offset, len(rows))
	end := min(start+req.Limit, len(rows))
	return &types.SearchResult{Chunks: rows[start:end], Total: total}, nil
}
func (s *deleteLiveIndex) DeleteChunks(ctx context.Context, condition map[string]any, _ string, kb string) (int64, error) {
	if err := ctx.Err(); err != nil {
		return 0, err
	}
	if s.pauseEntered != nil {
		s.pauseOnce.Do(func() { close(s.pauseEntered); <-s.pauseRelease })
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.failDelete {
		return 0, errors.New("controlled index failure")
	}
	var count int64
	for id, row := range s.rows {
		matches := row["kb_id"] == kb
		for key, value := range condition {
			text, _ := row[key].(string)
			if !deleteLiveMatches(text, value) {
				matches = false
			}
		}
		if matches {
			delete(s.rows, id)
			count++
		}
	}
	return count, nil
}
func (s *deleteLiveIndex) UpdateChunks(ctx context.Context, condition, values map[string]any, _ string, kb string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, row := range s.rows {
		matches := row["kb_id"] == kb
		for key, value := range condition {
			text, _ := row[key].(string)
			if !deleteLiveMatches(text, value) {
				matches = false
			}
		}
		if !matches {
			continue
		}
		for key, value := range values {
			if key == "remove" {
				for field, remove := range value.(map[string]any) {
					sources, err := documentGraphSources(row[field])
					if err != nil {
						return err
					}
					kept := []string{}
					for _, source := range sources {
						if source != remove {
							kept = append(kept, source)
						}
					}
					row[field] = kept
				}
			} else {
				row[key] = value
			}
		}
	}
	if _, removing := values["remove"]; removing && s.loseGraphUpdate {
		s.loseGraphUpdate = false
		return errors.New("controlled lost graph update acknowledgement")
	}
	return nil
}
func (s *deleteLiveIndex) DeleteMetadata(ctx context.Context, condition map[string]any, _ string) (int64, error) {
	if err := ctx.Err(); err != nil {
		return 0, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	var count int64
	for id, kb := range s.metadata {
		if condition["kb_id"] == kb && deleteLiveMatches(id, condition["id"]) {
			delete(s.metadata, id)
			count++
		}
	}
	return count, nil
}

type deleteLiveObjects struct {
	storage.Storage
	mu                         sync.Mutex
	bytes                      map[string]string
	uncertain                  bool
	calls                      int
	pauseEntered, pauseRelease chan struct{}
	pauseOnce                  sync.Once
}

func deleteObjectID(bucket, key string) string { return bucket + "\x00" + key }
func (s *deleteLiveObjects) RemoveContext(ctx context.Context, bucket, key string, _ ...string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if s.pauseEntered != nil {
		s.pauseOnce.Do(func() { close(s.pauseEntered); <-s.pauseRelease })
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.calls++
	delete(s.bytes, deleteObjectID(bucket, key))
	if s.uncertain {
		s.uncertain = false
		return errors.New("controlled lost object acknowledgement")
	}
	return nil
}
func (s *deleteLiveObjects) snapshot() map[string]string {
	s.mu.Lock()
	defer s.mu.Unlock()
	result := map[string]string{}
	for key, value := range s.bytes {
		result[key] = value
	}
	return result
}

type deleteLiveFixture struct {
	t                                                                            *testing.T
	db                                                                           *gorm.DB
	read                                                                         *sql.DB
	redis                                                                        *redis.Client
	journal                                                                      *testsupport.CaseJournal
	prefix, owner, kb, otherKB, a, b, foreign, taskA, taskB, fileA, fileB, queue string
	index                                                                        *deleteLiveIndex
	objects                                                                      *deleteLiveObjects
	service                                                                      *DocumentDeleteService
}

func documentDeleteLiveFixture(t *testing.T) *deleteLiveFixture {
	t.Helper()
	path := os.Getenv("MULTIRAG_AFTERFF_G_CONFIG")
	if path == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned SQL/Redis config required")
		}
		t.Skip("explicit owned SQL/Redis config required")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("private config unavailable")
	}
	var cfg struct {
		Database server.DatabaseConfig
		Redis    server.RedisConfig
	}
	if err := json.Unmarshal(data, &cfg); err != nil {
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
	journal.Event("borrow-services", map[string]any{"database": cfg.Database.Database, "sql_host": cfg.Database.Host, "sql_port": cfg.Database.Port, "schema": cfg.Database.Schema, "redis_host": cfg.Redis.Host, "redis_port": cfg.Redis.Port, "redis_db": cfg.Redis.DB})
	dsn, err := dao.PostgresDSN(cfg.Database)
	if err != nil {
		t.Fatal(err)
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
	if err := client.Ping(context.Background()).Err(); err != nil {
		t.Fatal("owned Redis unavailable")
	}
	journal.Event("reserve-schema", map[string]any{"database": cfg.Database.Database, "tables": []string{"t_ai_knowledgebases", "t_ai_documents", "t_ai_tasks", "t_ai_files", "t_ai_file2documents", "t_ai_user_tenants"}})
	if err := db.AutoMigrate(&entity.Knowledgebase{}, &entity.Document{}, &entity.Task{}, &entity.File{}, &entity.File2Document{}, &entity.UserTenant{}); err != nil {
		t.Fatal("owned schema unavailable")
	}
	prefix := "gdel" + strings.ReplaceAll(uuid.NewString(), "-", "")[:18]
	f := &deleteLiveFixture{t: t, db: db, read: read, redis: client, journal: journal, prefix: prefix}
	f.owner, f.kb, f.otherKB, f.a, f.b, f.foreign, f.taskA, f.taskB, f.fileA, f.fileB, f.queue = prefix+"owner", prefix+"kb", prefix+"other", prefix+"a", prefix+"b", prefix+"foreign", prefix+"ta", prefix+"tb", prefix+"fa", prefix+"fb", prefix+":queue"
	f.index = &deleteLiveIndex{datasets: map[string]bool{f.kb: true, f.otherKB: true}, rows: map[string]map[string]any{}, metadata: map[string]string{f.a: f.kb, f.b: f.kb}, expectedBase: map[string]string{f.kb: "multirag_" + f.owner, f.otherKB: "multirag_" + f.owner}}
	f.objects = &deleteLiveObjects{bytes: map[string]string{deleteObjectID(f.kb, "a.txt"): "source a", deleteObjectID(f.kb, "b.txt"): "source b", deleteObjectID(f.kb, "a-thumb"): "thumbnail a", deleteObjectID(f.kb, "a-image"): "image a", deleteObjectID(f.kb, "b-image"): "image b"}}
	f.service = NewDocumentDeleteService(db, client, f.index, f.objects, server.EngineElasticsearch)
	keys := []string{f.queue, f.taskA + "-cancel", f.taskB + "-cancel", taskRuntimePrefix + f.taskA, taskRuntimePrefix + f.taskB, "document-task-images:" + f.taskA, "document-task-images:" + f.taskB, "graphrag:writer:" + f.owner + ":" + f.kb, "document-source-recovery:" + f.a + ":" + f.taskA, "document-source-recovery:" + f.b + ":" + f.taskB}
	journal.Event("reserve-resources", map[string]any{"sql_ids": []string{f.owner, f.kb, f.otherKB, f.a, f.b, f.foreign, f.taskA, f.taskB, f.fileA, f.fileB, prefix + "la", prefix + "lb"}, "redis_keys": keys, "controlled_object_keys": []string{f.kb + "/a.txt", f.kb + "/b.txt", f.kb + "/a-thumb", f.kb + "/a-image", f.kb + "/b-image"}, "controlled_index_datasets": []string{f.kb, f.otherKB}})
	t.Cleanup(func() {
		ctx := context.Background()
		if err := db.Transaction(func(tx *gorm.DB) error {
			for _, row := range []struct {
				model any
				where string
				args  []any
			}{{&entity.File2Document{}, "document_id LIKE ?", []any{prefix + "%"}}, {&entity.Task{}, "doc_id LIKE ?", []any{prefix + "%"}}, {&entity.Document{}, "id LIKE ?", []any{prefix + "%"}}, {&entity.File{}, "id LIKE ?", []any{prefix + "%"}}, {&entity.UserTenant{}, "tenant_id = ?", []any{f.owner}}, {&entity.Knowledgebase{}, "id LIKE ?", []any{prefix + "%"}}} {
				if err := tx.Where(row.where, row.args...).Delete(row.model).Error; err != nil {
					return err
				}
			}
			return nil
		}); err != nil {
			t.Error("owned SQL cleanup failed")
		}
		if err := client.Del(ctx, keys...).Err(); err != nil {
			t.Error("owned Redis cleanup failed")
		}
		var count int
		if err := read.QueryRow("SELECT (SELECT count(*) FROM t_ai_documents WHERE id LIKE $1)+(SELECT count(*) FROM t_ai_tasks WHERE doc_id LIKE $1)+(SELECT count(*) FROM t_ai_files WHERE id LIKE $1)+(SELECT count(*) FROM t_ai_file2documents WHERE document_id LIKE $1)+(SELECT count(*) FROM t_ai_knowledgebases WHERE id LIKE $1)", prefix+"%").Scan(&count); err != nil || count != 0 {
			t.Error("SQL cleanup readback", count)
		}
		remaining, err := client.Exists(ctx, keys...).Result()
		if err != nil || remaining != 0 {
			t.Error("Redis cleanup readback", remaining)
		}
		journal.Event("cleanup-readback", map[string]any{"sql_remaining": count, "redis_remaining": remaining, "borrowed_services_preserved": true})
	})
	one := func(value string) *string { return &value }
	kbs := []entity.Knowledgebase{{ID: f.kb, TenantID: f.owner, CreatedBy: f.owner, Name: prefix, EmbdID: "", Permission: "team", Status: one("1"), DocNum: 2, ChunkNum: 2, TokenNum: 12, ParserConfig: entity.JSONMap{}}, {ID: f.otherKB, TenantID: f.owner, CreatedBy: f.owner, Name: prefix + "other", EmbdID: "", Permission: "me", Status: one("1"), DocNum: 1, ParserConfig: entity.JSONMap{}}}
	docs := []entity.Document{{ID: f.a, KbID: f.kb, CreatedBy: f.owner, Name: one("a"), ParserID: "naive", ParserConfig: entity.JSONMap{}, Type: "doc", SourceType: "local", Suffix: "txt", Location: one("a.txt"), Thumbnail: one("a-thumb"), ChunkNum: 1, TokenNum: 5, Run: one("1"), Status: one("1")}, {ID: f.b, KbID: f.kb, CreatedBy: f.owner, Name: one("b"), ParserID: "naive", ParserConfig: entity.JSONMap{}, Type: "doc", SourceType: "local", Suffix: "txt", Location: one("b.txt"), ChunkNum: 1, TokenNum: 7, Run: one("1"), Status: one("1")}, {ID: f.foreign, KbID: f.otherKB, CreatedBy: f.owner, Name: one("foreign"), ParserID: "naive", ParserConfig: entity.JSONMap{}, Type: "doc", Suffix: "txt", Status: one("1")}}
	files := []entity.File{{ID: f.fileA, ParentID: f.owner, TenantID: f.owner, CreatedBy: f.owner, Name: "a", Location: one("a.txt"), Type: "doc", SourceType: "knowledgebase"}, {ID: f.fileB, ParentID: f.owner, TenantID: f.owner, CreatedBy: f.owner, Name: "b", Location: one("b.txt"), Type: "doc", SourceType: "knowledgebase"}}
	for _, rows := range []any{&kbs, &docs, &files, &[]entity.Task{{ID: f.taskA, DocID: f.a, Progress: 0.4}, {ID: f.taskB, DocID: f.b, Progress: 0.2}}, &[]entity.File2Document{{ID: prefix + "la", FileID: &f.fileA, DocumentID: &f.a}, {ID: prefix + "lb", FileID: &f.fileB, DocumentID: &f.b}}} {
		if err := db.Create(rows).Error; err != nil {
			t.Fatal("owned SQL seeding failed")
		}
	}
	for _, row := range []map[string]any{{"id": prefix + "ia", "doc_id": f.a, "kb_id": f.kb, "img_id": f.kb + "-a-image"}, {"id": prefix + "ib", "doc_id": f.b, "kb_id": f.kb, "img_id": f.kb + "-b-image"}} {
		f.index.rows[row["id"].(string)] = row
	}
	if err := client.XAdd(context.Background(), &redis.XAddArgs{Stream: f.queue, Values: map[string]any{"id": f.taskA, "doc_id": f.a}}).Err(); err != nil {
		t.Fatal("owned queue unavailable")
	}
	return f
}

func (f *deleteLiveFixture) state() string {
	f.t.Helper()
	var result string
	query := `SELECT json_build_object('docs',(SELECT json_agg(to_jsonb(d) ORDER BY id) FROM t_ai_documents d WHERE id LIKE $1),'tasks',(SELECT json_agg(to_jsonb(t) ORDER BY id) FROM t_ai_tasks t WHERE doc_id LIKE $1),'links',(SELECT json_agg(to_jsonb(l) ORDER BY id) FROM t_ai_file2documents l WHERE document_id LIKE $1),'files',(SELECT json_agg(to_jsonb(f) ORDER BY id) FROM t_ai_files f WHERE id LIKE $1),'kb',(SELECT json_agg(to_jsonb(k) ORDER BY id) FROM t_ai_knowledgebases k WHERE id LIKE $1))::text`
	if err := f.read.QueryRow(query, f.prefix+"%").Scan(&result); err != nil {
		f.t.Fatal("independent SQL snapshot unavailable")
	}
	return result
}
func (f *deleteLiveFixture) assertCanceledParents() {
	f.t.Helper()
	var docs, tasks, links, files, canceled int
	var docNum, chunks, tokens int64
	if err := f.read.QueryRow("SELECT (SELECT count(*) FROM t_ai_documents WHERE id IN ($1,$2)),(SELECT count(*) FROM t_ai_tasks WHERE id IN ($3,$4)),(SELECT count(*) FROM t_ai_file2documents WHERE document_id IN ($1,$2)),(SELECT count(*) FROM t_ai_files WHERE id IN ($5,$6)),(SELECT count(*) FROM t_ai_tasks WHERE id IN ($3,$4) AND progress=-1 AND progress_msg LIKE '%[cancel_requested]%')", f.a, f.b, f.taskA, f.taskB, f.fileA, f.fileB).Scan(&docs, &tasks, &links, &files, &canceled); err != nil {
		f.t.Fatal(err)
	}
	if err := f.read.QueryRow("SELECT doc_num,chunk_num,token_num FROM t_ai_knowledgebases WHERE id=$1", f.kb).Scan(&docNum, &chunks, &tokens); err != nil {
		f.t.Fatal(err)
	}
	if docs != 2 || tasks != 2 || links != 2 || files != 2 || canceled != 2 || docNum != 2 || chunks != 2 || tokens != 12 {
		f.t.Fatalf("retry parents/counters changed: %d %d %d %d %d %d %d %d", docs, tasks, links, files, canceled, docNum, chunks, tokens)
	}
	for _, task := range []string{f.taskA, f.taskB} {
		value, err := f.redis.Get(context.Background(), task+"-cancel").Result()
		if err != nil || value == "" {
			f.t.Fatal("cancellation fence absent")
		}
	}
}
func (f *deleteLiveFixture) assertDeleted() {
	f.t.Helper()
	var count int
	var docs, chunks, tokens int64
	if err := f.read.QueryRow("SELECT (SELECT count(*) FROM t_ai_documents WHERE id IN ($1,$2))+(SELECT count(*) FROM t_ai_tasks WHERE id IN ($3,$4))+(SELECT count(*) FROM t_ai_file2documents WHERE document_id IN ($1,$2))+(SELECT count(*) FROM t_ai_files WHERE id IN ($5,$6))", f.a, f.b, f.taskA, f.taskB, f.fileA, f.fileB).Scan(&count); err != nil || count != 0 {
		f.t.Fatal("owned rows remain", count)
	}
	if err := f.read.QueryRow("SELECT doc_num,chunk_num,token_num FROM t_ai_knowledgebases WHERE id=$1", f.kb).Scan(&docs, &chunks, &tokens); err != nil || docs != 0 || chunks != 0 || tokens != 0 {
		f.t.Fatal("counter drift", docs, chunks, tokens)
	}
	if len(f.objects.snapshot()) != 0 || len(f.index.rows) != 0 || len(f.index.metadata) != 0 {
		f.t.Fatal("controlled external bytes remain")
	}
	var foreign int
	if err := f.read.QueryRow("SELECT count(*) FROM t_ai_documents WHERE id=$1", f.foreign).Scan(&foreign); err != nil || foreign != 1 {
		f.t.Fatal("sibling dataset changed")
	}
	for _, task := range []string{f.taskA, f.taskB} {
		value, err := f.redis.Get(context.Background(), task+"-cancel").Result()
		if err != nil || value == "" {
			f.t.Fatal("queued old token no longer fenced")
		}
	}
	queue, err := f.redis.XRange(context.Background(), f.queue, "-", "+").Result()
	if err != nil || len(queue) != 1 {
		f.t.Fatal("queue rewritten during deletion")
	}
	// A delayed producer cannot reacquire either authoritative parent. Actual
	// Python worker execution is a separate acceptance layer.
	if result := f.db.Model(&entity.Task{}).Where("id=?", f.taskA).Update("progress", 1); result.Error != nil || result.RowsAffected != 0 {
		f.t.Fatal("old task wrote completion")
	}
	f.journal.Event("business-readback", map[string]any{"retired_rows": count, "counters": []int64{docs, chunks, tokens}, "foreign_document_preserved": true, "queued_old_task_fenced": true, "controlled_objects": len(f.objects.snapshot()), "controlled_index_rows": len(f.index.rows)})
}

func TestDocumentDeleteLiveRejectsBeforeCancellation(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	before := f.state()
	objects := f.objects.snapshot()
	for _, test := range []struct {
		user string
		ids  []string
		all  bool
		want error
	}{{f.owner, []string{f.a, f.foreign}, false, ErrDocumentDeleteForbidden}, {f.prefix + "stranger", []string{f.a}, false, ErrDocumentDeleteForbidden}, {f.owner, nil, false, ErrInvalidDocumentDeleteRequest}, {f.owner, []string{}, true, ErrInvalidDocumentDeleteRequest}} {
		if _, err := f.service.DeleteDocuments(context.Background(), f.kb, test.user, test.ids, test.all); !errors.Is(err, test.want) {
			t.Fatal(err)
		}
		if f.state() != before || !reflect.DeepEqual(objects, f.objects.snapshot()) || f.objects.calls != 0 {
			t.Fatal("denial caused side effects")
		}
		if count, err := f.redis.Exists(context.Background(), f.taskA+"-cancel", f.taskB+"-cancel").Result(); err != nil || count != 0 {
			t.Fatal("denial wrote cancellation")
		}
	}
	if count, err := f.service.DeleteDocuments(context.Background(), f.kb, f.owner, []string{}, false); err != nil || count != 0 || f.state() != before {
		t.Fatal("[] broadened selection", count, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := f.service.DeleteDocuments(ctx, f.kb, f.owner, []string{f.a}, false); !errors.Is(err, context.Canceled) || f.state() != before {
		t.Fatal("canceled request wrote SQL", err)
	}
}

func TestDocumentDeleteLiveCancellationFailureCompensatesOwnNonce(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	before := f.state()
	constraint := f.prefix + "guard"
	f.journal.Event("reserve-constraint", map[string]any{"table": "t_ai_tasks", "constraint": constraint})
	if err := f.db.Exec(fmt.Sprintf("ALTER TABLE t_ai_tasks ADD CONSTRAINT %s CHECK (id <> '%s' OR progress >= 0)", constraint, f.taskB)).Error; err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := f.db.Exec("ALTER TABLE t_ai_tasks DROP CONSTRAINT " + constraint).Error; err != nil {
			t.Error(err)
		}
	})
	if _, err := f.service.DeleteDocuments(context.Background(), f.kb, f.owner, []string{f.a, f.b}, false); !errors.Is(err, ErrDocumentDeleteRetryable) {
		t.Fatal(err)
	}
	if f.state() != before || f.objects.calls != 0 {
		t.Fatal("failed cancellation committed SQL/external deletion")
	}
	if count, err := f.redis.Exists(context.Background(), f.taskA+"-cancel", f.taskB+"-cancel").Result(); err != nil || count != 0 {
		t.Fatal("own rollback nonce leaked")
	}
}

func TestDocumentDeleteLiveUnknownObjectAndIndexFailuresRemainRetryable(t *testing.T) {
	for _, boundary := range []string{"object", "index"} {
		t.Run(boundary, func(t *testing.T) {
			f := documentDeleteLiveFixture(t)
			if boundary == "object" {
				f.objects.uncertain = true
			} else {
				f.index.failDelete = true
			}
			if count, err := f.service.DeleteDocuments(context.Background(), f.kb, f.owner, []string{f.b, f.a, f.a}, false); count != 0 || !errors.Is(err, ErrDocumentDeleteRetryable) {
				t.Fatal(count, err)
			}
			f.assertCanceledParents()
			f.index.failDelete = false
			if count, err := f.service.DeleteDocuments(context.Background(), f.kb, f.owner, []string{f.a, f.b}, false); count != 2 || err != nil {
				t.Fatal(count, err)
			}
			f.assertDeleted()
		})
	}
}

func TestDocumentDeleteLiveFinalSQLFailureRollsBackAllCounters(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	function, trigger := f.prefix+"fn", f.prefix+"trigger"
	f.journal.Event("reserve-trigger", map[string]any{"table": "t_ai_documents", "function": function, "trigger": trigger})
	statement := fmt.Sprintf("CREATE FUNCTION %s() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF OLD.id='%s' THEN RAISE EXCEPTION 'controlled final deletion failure'; END IF; RETURN OLD; END $$; CREATE TRIGGER %s BEFORE DELETE ON t_ai_documents FOR EACH ROW EXECUTE FUNCTION %s()", function, f.b, trigger, function)
	if err := f.db.Exec(statement).Error; err != nil {
		t.Fatal(err)
	}
	dropped := false
	t.Cleanup(func() {
		if !dropped {
			if err := f.db.Exec("DROP TRIGGER " + trigger + " ON t_ai_documents; DROP FUNCTION " + function + "()").Error; err != nil {
				t.Error(err)
			}
		}
	})
	if count, err := f.service.DeleteDocuments(context.Background(), f.kb, f.owner, []string{f.a, f.b}, false); count != 0 || !errors.Is(err, ErrDocumentDeleteRetryable) {
		t.Fatal(count, err)
	}
	f.assertCanceledParents()
	if err := f.db.Exec("DROP TRIGGER " + trigger + " ON t_ai_documents; DROP FUNCTION " + function + "()").Error; err != nil {
		t.Fatal(err)
	}
	dropped = true
	if count, err := f.service.DeleteDocuments(context.Background(), f.kb, f.owner, nil, true); count != 2 || err != nil {
		t.Fatal(count, err)
	}
	f.assertDeleted()
}

func TestDocumentDeleteLivePreservesSharedFileImageAndPendingWriter(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	if err := f.db.Model(&entity.File2Document{}).Where("document_id=?", f.b).Update("file_id", f.fileA).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.db.Model(&entity.Document{}).Where("id=?", f.b).Update("location", "a.txt").Error; err != nil {
		t.Fatal(err)
	}
	f.index.rows[f.prefix+"ib"]["img_id"] = f.kb + "-a-image"
	member, _ := json.Marshal([]string{f.kb, "a-thumb"})
	f.journal.Event("reserve-redis-member", map[string]any{"key": "document-task-images:" + f.taskB, "member": string(member)})
	if err := f.redis.SAdd(context.Background(), "document-task-images:"+f.taskB, string(member)).Err(); err != nil {
		t.Fatal(err)
	}
	if count, err := f.service.DeleteDocuments(context.Background(), f.kb, f.owner, []string{f.a}, false); count != 1 || err != nil {
		t.Fatal(count, err)
	}
	for _, key := range []string{"a.txt", "a-image", "a-thumb"} {
		if f.objects.snapshot()[deleteObjectID(f.kb, key)] == "" {
			t.Fatal("shared object deleted", key)
		}
	}
	var files, docs, links int
	if err := f.read.QueryRow("SELECT (SELECT count(*) FROM t_ai_files WHERE id=$1),(SELECT count(*) FROM t_ai_documents WHERE id=$2),(SELECT count(*) FROM t_ai_file2documents WHERE file_id=$1)", f.fileA, f.b).Scan(&files, &docs, &links); err != nil || files != 1 || docs != 1 || links != 1 {
		t.Fatal("shared SQL identity changed", files, docs, links)
	}
	if len(f.index.rows) != 1 || len(f.index.metadata) != 1 {
		t.Fatal("shared index scope changed")
	}
	f.journal.Event("shared-readback", map[string]any{"file_rows": files, "sibling_document": docs, "shared_links": links, "image_preserved": true, "pending_writer_bytes_preserved": true})
}

func TestDocumentDeleteLiveMissingFileAndRecoveryMirrorsBlockBeforeCancel(t *testing.T) {
	for _, boundary := range []string{"file", "null-file", "empty-file", "multiple-files", "invalid-source", "mirror"} {
		t.Run(boundary, func(t *testing.T) {
			f := documentDeleteLiveFixture(t)
			if boundary == "file" {
				if err := f.db.Where("id=?", f.fileA).Delete(&entity.File{}).Error; err != nil {
					t.Fatal(err)
				}
			} else if boundary == "null-file" || boundary == "empty-file" {
				var value any
				if boundary == "empty-file" {
					value = ""
				}
				if err := f.db.Model(&entity.File2Document{}).Where("document_id=?", f.a).Update("file_id", value).Error; err != nil {
					t.Fatal(err)
				}
			} else if boundary == "invalid-source" {
				if err := f.db.Model(&entity.Document{}).Where("id=?", f.a).Update("location", "../foreign-object").Error; err != nil {
					t.Fatal(err)
				}
			} else if boundary == "multiple-files" {
				f.journal.Event("reserve-sql-link", map[string]any{"id": f.prefix + "ambiguous", "document_id": f.a, "file_id": f.fileB})
				if err := f.db.Create(&entity.File2Document{ID: f.prefix + "ambiguous", FileID: &f.fileB, DocumentID: &f.a}).Error; err != nil {
					t.Fatal(err)
				}
			} else {
				key := "document-source-recovery:" + f.a + ":" + f.taskA
				f.journal.Event("reserve-recovery-mirror", map[string]any{"key": key})
				if err := f.redis.Set(context.Background(), key, "unknown recovery wire", 0).Err(); err != nil {
					t.Fatal(err)
				}
			}
			before := f.state()
			if _, err := f.service.DeleteDocuments(context.Background(), f.kb, f.owner, []string{f.a}, false); !errors.Is(err, ErrDocumentDeleteRetryable) {
				t.Fatal(err)
			}
			if f.state() != before || f.objects.calls != 0 {
				t.Fatal("unresolved source caused side effects")
			}
			if count, err := f.redis.Exists(context.Background(), f.taskA+"-cancel", f.taskB+"-cancel").Result(); err != nil || count != 0 {
				t.Fatal("unresolved source wrote cancel token")
			}
			if boundary == "mirror" {
				value, err := f.redis.Get(context.Background(), "document-source-recovery:"+f.a+":"+f.taskA).Result()
				if err != nil || value != "unknown recovery wire" {
					t.Fatal("unknown wire was changed")
				}
			}
		})
	}
}

func TestDocumentDeleteLiveGraphLostAckInvalidatesBeforeRetry(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	f.index.rows[f.prefix+"graph"] = map[string]any{"id": f.prefix + "graph", "kb_id": f.kb, "knowledge_graph_kwd": "graph", "source_id": []string{f.a, f.b}, "removed_kwd": "N", "content_with_weight": "cached aggregate still includes removed source"}
	f.index.loseGraphUpdate = true
	if count, err := f.service.DeleteDocuments(context.Background(), f.kb, f.owner, []string{f.a}, false); count != 0 || !errors.Is(err, ErrDocumentDeleteRetryable) {
		t.Fatal(count, err)
	}
	if f.index.rows[f.prefix+"graph"]["removed_kwd"] != "Y" {
		t.Fatal("aggregate stayed available after a lost provenance update acknowledgement")
	}
	var parents int
	if err := f.read.QueryRow("SELECT (SELECT count(*) FROM t_ai_documents WHERE id=$1)+(SELECT count(*) FROM t_ai_tasks WHERE id=$2 AND progress=-1)", f.a, f.taskA).Scan(&parents); err != nil || parents != 2 {
		t.Fatal("failed graph cleanup lost retry parents")
	}
	if count, err := f.service.DeleteDocuments(context.Background(), f.kb, f.owner, []string{f.a}, false); count != 1 || err != nil {
		t.Fatal(count, err)
	}
	sources, err := documentGraphSources(f.index.rows[f.prefix+"graph"]["source_id"])
	if err != nil || !reflect.DeepEqual(sources, []string{f.b}) || f.index.rows[f.prefix+"graph"]["removed_kwd"] != "Y" {
		t.Fatal("graph retry revived stale aggregate", sources, err)
	}
	f.journal.Event("graph-lost-ack-readback", map[string]any{"aggregate_invalid_before_source_mutation": true, "shared_source_preserved": true, "retry_retired_SQL": true})
}

func TestDocumentDeleteLiveAllSelectionReadsEveryPage(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	ids := []string{}
	documents := []entity.Document{}
	for index := 0; index < documentDeletePageSize+12; index++ {
		id := fmt.Sprintf("%sn%03d", f.prefix, index)
		ids = append(ids, id)
		documents = append(documents, entity.Document{ID: id, KbID: f.kb, CreatedBy: f.owner, ParserID: "naive", ParserConfig: entity.JSONMap{}, Type: "doc", Suffix: "txt"})
	}
	f.journal.Event("reserve-sql-documents", map[string]any{"dataset_id": f.kb, "ids": ids})
	if err := f.db.CreateInBatches(&documents, documentDeletePageSize).Error; err != nil {
		t.Fatal(err)
	}
	total := len(documents) + 2
	if err := f.db.Model(&entity.Knowledgebase{}).Where("id=?", f.kb).Update("doc_num", total).Error; err != nil {
		t.Fatal(err)
	}
	if count, err := f.service.DeleteDocuments(context.Background(), f.kb, f.owner, nil, true); count != total || err != nil {
		t.Fatal("delete_all lost a page", count, total, err)
	}
	var remaining int
	if err := f.read.QueryRow("SELECT count(*) FROM t_ai_documents WHERE kb_id=$1", f.kb).Scan(&remaining); err != nil || remaining != 0 {
		t.Fatal("delete_all remaining SQL", remaining)
	}
	f.assertDeleted()
	f.journal.Event("all-pages-readback", map[string]any{"selected": total, "remaining": remaining, "page_size": documentDeletePageSize})
}

func TestDocumentDeleteLiveAttachWaitsForFileRetirement(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	survivor := f.sameDatasetSurvivor()
	f.objects.pauseEntered, f.objects.pauseRelease = make(chan struct{}), make(chan struct{})
	var release sync.Once
	unblock := func() { release.Do(func() { close(f.objects.pauseRelease) }) }
	t.Cleanup(unblock)
	type outcome struct {
		count int
		err   error
	}
	deleted := make(chan outcome, 1)
	go func() {
		count, err := f.service.DeleteDocuments(context.Background(), f.kb, f.owner, []string{f.a}, false)
		deleted <- outcome{count, err}
	}()
	select {
	case <-f.objects.pauseEntered:
	case <-time.After(10 * time.Second):
		t.Fatal("object boundary not reached")
	}
	mappingID := f.prefix + "attach"
	f.journal.Event("reserve-sql-attach", map[string]any{"mapping_id": mappingID, "file_id": f.fileA, "document_id": survivor, "dataset_id": f.kb})
	started, attached := make(chan int, 1), make(chan error, 1)
	go func() {
		attached <- f.db.Transaction(func(tx *gorm.DB) error {
			var pid int
			if err := tx.Raw("SELECT pg_backend_pid()").Scan(&pid).Error; err != nil {
				return err
			}
			started <- pid
			return dao.NewFile2DocumentDAO().CreateInTransaction(tx, &entity.File2Document{ID: mappingID, FileID: &f.fileA, DocumentID: &survivor})
		})
	}()
	var pid int
	select {
	case pid = <-started:
	case <-time.After(10 * time.Second):
		t.Fatal("attach transaction did not start")
	}
	waiting := false
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		select {
		case err := <-attached:
			t.Fatal("attach returned before file retirement", err)
		default:
		}
		var eventType, query string
		if err := f.read.QueryRow("SELECT COALESCE(wait_event_type,''),query FROM pg_stat_activity WHERE pid=$1", pid).Scan(&eventType, &query); err != nil {
			t.Fatal("independent lock wait readback unavailable")
		}
		if eventType == "Lock" && (strings.Contains(query, "t_ai_files") || strings.Contains(query, "t_ai_knowledgebases")) {
			waiting = true
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if !waiting {
		t.Fatal("attach did not wait on the shared dataset/file lock protocol")
	}
	f.journal.Event("attach-lock-wait-readback", map[string]any{"backend_pid": pid, "file_id": f.fileA, "waiting": waiting})
	unblock()
	select {
	case result := <-deleted:
		if result.count != 1 || result.err != nil {
			t.Fatal(result.count, result.err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("delete did not finish")
	}
	select {
	case err := <-attached:
		if !errors.Is(err, gorm.ErrRecordNotFound) {
			t.Fatal("attach did not reject retired File", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("attach did not drain")
	}
	var remaining int
	if err := f.read.QueryRow("SELECT (SELECT count(*) FROM t_ai_documents WHERE id=$1)+(SELECT count(*) FROM t_ai_files WHERE id=$2)+(SELECT count(*) FROM t_ai_file2documents WHERE id=$3)", f.a, f.fileA, mappingID).Scan(&remaining); err != nil || remaining != 0 {
		t.Fatal("retirement or dangling link readback failed", remaining)
	}
	if _, exists := f.objects.snapshot()[deleteObjectID(f.kb, "a.txt")]; exists {
		t.Fatal("unshared source blob was not removed")
	}
	f.assertSurvivorCounters(survivor)
	f.journal.Event("attach-after-retirement-readback", map[string]any{"dangling_links": remaining, "source_blob_absent": true, "actual_DAO_lock_protocol": true, "same_dataset_survivor": survivor})
}

func TestDocumentDeleteLiveEarlierAttachProtectsFileAndSource(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	survivor := f.sameDatasetSurvivor()
	mappingID := f.prefix + "attach"
	f.journal.Event("reserve-sql-attach", map[string]any{"mapping_id": mappingID, "file_id": f.fileA, "document_id": survivor, "dataset_id": f.kb})
	if err := f.db.Transaction(func(tx *gorm.DB) error {
		return dao.NewFile2DocumentDAO().CreateInTransaction(tx, &entity.File2Document{ID: mappingID, FileID: &f.fileA, DocumentID: &survivor})
	}); err != nil {
		t.Fatal(err)
	}
	if count, err := f.service.DeleteDocuments(context.Background(), f.kb, f.owner, []string{f.a}, false); count != 1 || err != nil {
		t.Fatal(count, err)
	}
	var file, link int
	if err := f.read.QueryRow("SELECT (SELECT count(*) FROM t_ai_files WHERE id=$1),(SELECT count(*) FROM t_ai_file2documents WHERE id=$2)", f.fileA, mappingID).Scan(&file, &link); err != nil || file != 1 || link != 1 {
		t.Fatal("earlier attachment lost registered file", file, link)
	}
	if _, exists := f.objects.snapshot()[deleteObjectID(f.kb, "a.txt")]; !exists {
		t.Fatal("earlier attachment lost source bytes")
	}
	f.assertSurvivorCounters(survivor)
	f.journal.Event("attach-before-retirement-readback", map[string]any{"file_rows": file, "link_rows": link, "source_preserved": true, "actual_DAO_lock_protocol": true, "same_dataset_survivor": survivor})
}

func (f *deleteLiveFixture) sameDatasetSurvivor() string {
	f.t.Helper()
	id := f.prefix + "survivor"
	f.journal.Event("reserve-sql-survivor", map[string]any{"id": id, "dataset_id": f.kb})
	if err := f.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Create(&entity.Document{ID: id, KbID: f.kb, CreatedBy: f.owner, ParserID: "naive", ParserConfig: entity.JSONMap{}, Type: "doc", Suffix: "txt"}).Error; err != nil {
			return err
		}
		return tx.Model(&entity.Knowledgebase{}).Where("id=?", f.kb).Update("doc_num", gorm.Expr("doc_num + 1")).Error
	}); err != nil {
		f.t.Fatal(err)
	}
	return id
}

func (f *deleteLiveFixture) assertSurvivorCounters(survivor string) {
	f.t.Helper()
	var docs, chunks, tokens, survivors int64
	if err := f.read.QueryRow("SELECT doc_num,chunk_num,token_num,(SELECT count(*) FROM t_ai_documents WHERE id=$2) FROM t_ai_knowledgebases WHERE id=$1", f.kb, survivor).Scan(&docs, &chunks, &tokens, &survivors); err != nil {
		f.t.Fatal(err)
	}
	if docs != 2 || chunks != 1 || tokens != 7 || survivors != 1 {
		f.t.Fatal("same-dataset survivor or counters changed", docs, chunks, tokens, survivors)
	}
	f.journal.Event("same-dataset-count-readback", map[string]any{"doc_num": docs, "chunk_num": chunks, "token_num": tokens, "survivor_rows": survivors})
}

func (f *deleteLiveFixture) seedSourceRecovery(phase string) {
	f.t.Helper()
	var present bool
	if err := f.db.Raw("SELECT to_regclass('t_document_source_recovery') IS NOT NULL").Scan(&present).Error; err != nil {
		f.t.Fatal(err)
	}
	f.journal.Event("reserve-source-recovery", map[string]any{"table": "t_document_source_recovery", "id": f.prefix + "recovery", "document_id": f.a, "task_id": f.taskA, "create_table": !present})
	if !present {
		if err := f.db.Exec("CREATE TABLE t_document_source_recovery (id varchar(32) PRIMARY KEY,document_id varchar(32) NOT NULL,task_id varchar(32) NOT NULL,wire text NOT NULL)").Error; err != nil {
			f.t.Fatal(err)
		}
	}
	f.t.Cleanup(func() {
		if err := f.db.Exec("DELETE FROM t_document_source_recovery WHERE id=?", f.prefix+"recovery").Error; err != nil {
			f.t.Error(err)
		}
		var count int
		if err := f.read.QueryRow("SELECT count(*) FROM t_document_source_recovery WHERE id=$1", f.prefix+"recovery").Scan(&count); err != nil || count != 0 {
			f.t.Error("source recovery cleanup readback", count)
		}
		if !present {
			if err := f.db.Exec("DROP TABLE t_document_source_recovery").Error; err != nil {
				f.t.Error(err)
			}
		}
		f.journal.Event("recovery-cleanup-readback", map[string]any{"rows": count, "created_table_removed": !present})
	})
	wire, _ := json.Marshal(map[string]any{"payload": map[string]any{"version": 1, "document_id": f.a, "task_id": f.taskA, "nonce": f.prefix + "recovery", "phase": phase}, "types": []any{}})
	if err := f.db.Exec("INSERT INTO t_document_source_recovery (id,document_id,task_id,wire) VALUES (?,?,?,?)", f.prefix+"recovery", f.a, f.taskA, string(wire)).Error; err != nil {
		f.t.Fatal(err)
	}
}

func TestDocumentDeleteLivePendingSQLRecoveryBlocksAndReadyRetires(t *testing.T) {
	for _, phase := range []string{"unknown", "ready"} {
		t.Run(phase, func(t *testing.T) {
			f := documentDeleteLiveFixture(t)
			f.seedSourceRecovery(phase)
			before := f.state()
			count, err := f.service.DeleteDocuments(context.Background(), f.kb, f.owner, []string{f.a, f.b}, false)
			if phase == "unknown" {
				if count != 0 || !errors.Is(err, ErrDocumentDeleteRetryable) || f.state() != before || f.objects.calls != 0 {
					t.Fatal("pending SQL recovery did not block", count, err)
				}
				if count, err := f.redis.Exists(context.Background(), f.taskA+"-cancel").Result(); err != nil || count != 0 {
					t.Fatal("pending SQL recovery wrote cancel")
				}
				var records int
				if err := f.read.QueryRow("SELECT count(*) FROM t_document_source_recovery WHERE id=$1", f.prefix+"recovery").Scan(&records); err != nil || records != 1 {
					t.Fatal("pending SQL recovery was removed")
				}
			} else {
				if count != 2 || err != nil {
					t.Fatal(count, err)
				}
				f.assertDeleted()
				var records int
				if err := f.read.QueryRow("SELECT count(*) FROM t_document_source_recovery WHERE id=$1", f.prefix+"recovery").Scan(&records); err != nil || records != 0 {
					t.Fatal("ready SQL recovery was not retired")
				}
			}
		})
	}
}

func TestDocumentDeleteLiveCanceledHTTPDrainsNativeBeforeUnlock(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	f.index.pauseEntered, f.index.pauseRelease = make(chan struct{}), make(chan struct{})
	var release sync.Once
	unblock := func() { release.Do(func() { close(f.index.pauseRelease) }) }
	t.Cleanup(unblock)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	type outcome struct {
		count int
		err   error
	}
	deleted := make(chan outcome, 1)
	go func() {
		count, err := f.service.DeleteDocuments(ctx, f.kb, f.owner, []string{f.a, f.b}, false)
		deleted <- outcome{count, err}
	}()
	select {
	case <-f.index.pauseEntered:
	case <-time.After(10 * time.Second):
		t.Fatal("native boundary not reached")
	}
	cancel()
	f.journal.Event("native-drain-cancel", map[string]any{"document_id": f.a})
	started, worker := make(chan struct{}), make(chan error, 1)
	go func() {
		worker <- f.db.Transaction(func(tx *gorm.DB) error {
			close(started)
			var doc entity.Document
			return tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id=?", f.a).Take(&doc).Error
		})
	}()
	<-started
	select {
	case err := <-worker:
		t.Fatal("document lock released while native deletion was pending", err)
	case <-time.After(150 * time.Millisecond):
	}
	unblock()
	select {
	case result := <-deleted:
		if result.count != 2 || result.err != nil {
			t.Fatal(result.count, result.err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("native drain did not finish")
	}
	select {
	case err := <-worker:
		if !errors.Is(err, gorm.ErrRecordNotFound) {
			t.Fatal("replacement writer did not observe retired parent", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("replacement writer did not drain")
	}
	f.assertDeleted()
	f.journal.Event("native-drain-readback", map[string]any{"HTTP_canceled": true, "SQL_lock_held_until_native_complete": true, "replacement_observed_retired_document": true})
}

func TestDocumentDeleteLiveSharedGraphAndZeroCounterRows(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	if err := f.db.Model(&entity.Document{}).Where("id=?", f.a).Update("chunk_num", 0).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.db.Model(&entity.Knowledgebase{}).Where("id=?", f.kb).Update("chunk_num", 1).Error; err != nil {
		t.Fatal(err)
	}
	f.index.rows[f.prefix+"entity"] = map[string]any{"id": f.prefix + "entity", "kb_id": f.kb, "knowledge_graph_kwd": "entity", "source_id": []string{f.a, f.b}}
	f.index.rows[f.prefix+"subgraph"] = map[string]any{"id": f.prefix + "subgraph", "kb_id": f.kb, "knowledge_graph_kwd": "subgraph", "source_id": []string{f.a}}
	f.index.rows[f.prefix+"graph"] = map[string]any{"id": f.prefix + "graph", "kb_id": f.kb, "knowledge_graph_kwd": "graph", "source_id": []string{f.a, f.b}, "removed_kwd": "N"}
	if count, err := f.service.DeleteDocuments(context.Background(), f.kb, f.owner, []string{f.a}, false); count != 1 || err != nil {
		t.Fatal(count, err)
	}
	if _, exists := f.index.rows[f.prefix+"ia"]; exists {
		t.Fatal("zero SQL chunk count suppressed index deletion")
	}
	if _, exists := f.index.rows[f.prefix+"subgraph"]; exists {
		t.Fatal("unreferenced subgraph remains")
	}
	for _, kind := range []string{"entity", "graph"} {
		row := f.index.rows[f.prefix+kind]
		sources, err := documentGraphSources(row["source_id"])
		if err != nil || !reflect.DeepEqual(sources, []string{f.b}) {
			t.Fatal("shared graph was deleted or retained old source", kind, sources, err)
		}
	}
	if f.index.rows[f.prefix+"graph"]["removed_kwd"] != "Y" {
		t.Fatal("aggregate graph was not invalidated")
	}
	f.journal.Event("graph-scope-readback", map[string]any{"shared_entity_preserved": true, "removed_source_absent": true, "unreferenced_subgraph_absent": true, "zero_counter_row_deleted": true, "shared_index_base": "multirag_" + f.owner})
}

func TestDocumentDeleteLiveMinioStorage(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	data, err := os.ReadFile(os.Getenv("MULTIRAG_AFTERFF_G_CONFIG"))
	if err != nil {
		t.Fatal("private storage config unavailable")
	}
	var cfg struct {
		StorageEngine struct {
			Minio *server.MinioConfig `json:"minio"`
		} `json:"storage_engine"`
	}
	if err := json.Unmarshal(data, &cfg); err != nil || cfg.StorageEngine.Minio == nil {
		t.Fatal("real MinIO configuration required")
	}
	options := *cfg.StorageEngine.Minio
	if options.Bucket != "" || options.PrefixPath != "" {
		t.Fatal("shared bucket/prefix forbidden for owned storage acceptance")
	}
	keys := []string{"a.txt", "b.txt", "a-thumb", "a-image", "b-image"}
	f.journal.Event("reserve-minio-bucket", map[string]any{"endpoint": options.Host, "bucket": f.kb, "keys": keys})
	reader, err := minio.New(options.Host, &minio.Options{Creds: credentials.NewStaticV4(options.User, options.Password, ""), Secure: options.Secure, Region: options.Region})
	if err != nil {
		t.Fatal("independent MinIO reader unavailable")
	}
	t.Cleanup(func() {
		ctx := context.Background()
		for _, key := range keys {
			if err := reader.RemoveObject(ctx, f.kb, key, minio.RemoveObjectOptions{}); err != nil {
				t.Error("owned object cleanup failed")
			}
		}
		if err := reader.RemoveBucket(ctx, f.kb); err != nil {
			t.Error("owned bucket cleanup failed")
		}
		exists, err := reader.BucketExists(ctx, f.kb)
		if err != nil || exists {
			t.Error("owned bucket cleanup readback failed")
		}
		f.journal.Event("minio-cleanup-readback", map[string]any{"bucket": f.kb, "absent": !exists && err == nil})
	})
	store, err := storage.NewMinioStorage(&options)
	if err != nil {
		t.Fatal("real MinIO storage unavailable")
	}
	for _, key := range keys {
		if err := store.Put(f.kb, key, []byte("owned deletion "+key)); err != nil {
			t.Fatal("owned object creation failed")
		}
		if _, err := reader.StatObject(context.Background(), f.kb, key, minio.StatObjectOptions{}); err != nil {
			t.Fatal("independent pre-delete object readback failed")
		}
	}
	f.service = NewDocumentDeleteService(f.db, f.redis, f.index, store, server.EngineElasticsearch)
	if count, err := f.service.DeleteDocuments(context.Background(), f.kb, f.owner, []string{f.a, f.b}, false); count != 2 || err != nil {
		t.Fatal(count, err)
	}
	for _, key := range keys {
		_, err := reader.StatObject(context.Background(), f.kb, key, minio.StatObjectOptions{})
		code := minio.ToErrorResponse(err).Code
		if err == nil || (code != "NoSuchKey" && code != "NoSuchObject") {
			t.Fatal("object absence not confirmed", code)
		}
	}
	f.objects.bytes = map[string]string{}
	f.assertDeleted()
	f.journal.Event("minio-business-readback", map[string]any{"bucket": f.kb, "keys_absent": keys, "reader_independent": true, "index_boundary": "controlled"})
}
