package service

import (
	"errors"
	"github.com/redis/go-redis/v9"
	"gorm.io/gorm"
	rediscache "multirag/internal/engine/redis"
	"multirag/internal/entity"
	"testing"
)

func canonicalStop(t *testing.T) (*canonicalLinkFixture, []entity.Document) {
	t.Helper()
	f := canonicalLink(t)
	if err := rediscache.Init(&f.Config.Redis); err != nil {
		t.Fatal("initialize owned Redis")
	}
	t.Cleanup(func() { rediscache.Close() })
	if err := f.service.convertFiles([]string{f.one, f.two}, []string{f.a}, f.owner); err != nil {
		t.Fatal(err)
	}
	docs := canonicalLinkedDocs(t, f, f.a)
	for _, doc := range docs {
		if err := f.DB.Model(&entity.Document{}).Where("id=?", doc.ID).Update("run", "1").Error; err != nil {
			t.Fatal(err)
		}
		if err := f.DB.Create(&entity.Task{ID: doc.ID, DocID: doc.ID, Progress: 0}).Error; err != nil {
			t.Fatal(err)
		}
	}
	return f, docs
}
func TestDocumentStopLiveDeduplicatedBatch(t *testing.T) {
	f, docs := canonicalStop(t)
	queue := f.Prefix + "queue"
	f.Journal.Event("reserve-stop-queue", map[string]any{"stream": queue})
	if err := f.Redis.XAdd(t.Context(), &redis.XAddArgs{Stream: queue, Values: map[string]any{"id": docs[0].ID, "doc_id": docs[0].ID}}).Err(); err != nil {
		t.Fatal(err)
	}
	result, err := f.service.documentSvc.StopParseDocuments(f.a, []string{docs[0].ID, docs[1].ID, docs[0].ID})
	if err != nil || result["success_count"] != 2 {
		t.Fatal(result, err)
	}
	var canceled, tasks int
	if err := f.Read.QueryRow("SELECT count(*) FROM document WHERE kb_id=? AND run='2'", f.a).Scan(&canceled); err != nil || canceled != 2 {
		t.Fatal(canceled, err)
	}
	if err := f.Read.QueryRow("SELECT count(*) FROM task").Scan(&tasks); err != nil || tasks != 2 {
		t.Fatal(tasks, err)
	}
	for _, doc := range docs {
		if value := f.Redis.Get(t.Context(), doc.ID+"-cancel").Val(); value != "x" {
			t.Fatal("canonical task cancel signal missing")
		}
	}
	if length := f.Redis.XLen(t.Context(), queue).Val(); length != 1 {
		t.Fatal("stop deleted queue envelope", length)
	}
	f.counts(t, f.a, 2)
	if keys := f.ObjectKeys(t); len(keys) != 2 {
		t.Fatal("stop changed source objects", keys)
	}
	if repeated, err := f.service.documentSvc.StopParseDocuments(f.a, []string{docs[0].ID, docs[1].ID}); err != nil || repeated["success_count"] != 2 {
		t.Fatal(repeated, err)
	}
}
func TestDocumentStopLiveRejectsEntireInvalidSelection(t *testing.T) {
	f, docs := canonicalStop(t)
	for _, ids := range [][]string{nil, {}, {docs[0].ID, "missing"}} {
		if result, err := f.service.documentSvc.StopParseDocuments(f.a, ids); err == nil || result != nil {
			t.Fatal("invalid stop selection accepted", result, err)
		}
	}
	if count := f.Redis.Exists(t.Context(), docs[0].ID+"-cancel", docs[1].ID+"-cancel").Val(); count != 0 {
		t.Fatal("invalid selection wrote cancel flags", count)
	}
	var running int
	if err := f.Read.QueryRow("SELECT count(*) FROM document WHERE kb_id=? AND run='1'", f.a).Scan(&running); err != nil || running != 2 {
		t.Fatal(running, err)
	}
}
func TestDocumentStopLiveSQLFailureCompensatesSignal(t *testing.T) {
	f, docs := canonicalStop(t)
	failure := errors.New("controlled stop write failure")
	if err := f.DB.Callback().Update().Before("gorm:update").Register("canonical-stop-fail", func(tx *gorm.DB) {
		if tx.Statement.Schema != nil && tx.Statement.Schema.Name == "Document" {
			tx.AddError(failure)
		}
	}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { f.DB.Callback().Update().Remove("canonical-stop-fail") })
	result, err := f.service.documentSvc.StopParseDocuments(f.a, []string{docs[0].ID})
	if err != nil || result["success_count"] != 0 || len(result["errors"].([]string)) != 1 {
		t.Fatal("failed stop falsely acknowledged", result, err)
	}
	if count := f.Redis.Exists(t.Context(), docs[0].ID+"-cancel").Val(); count != 0 {
		t.Fatal("failed SQL stop left a cancellation signal")
	}
	var run string
	if err := f.Read.QueryRow("SELECT run FROM document WHERE id=?", docs[0].ID).Scan(&run); err != nil || run != "1" {
		t.Fatal(run, err)
	}
}
