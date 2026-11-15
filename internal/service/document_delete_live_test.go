package service

import (
	"context"
	"errors"
	"gorm.io/gorm"
	"multirag/internal/engine"
	"multirag/internal/entity"
	"multirag/internal/storage"
	"testing"
)

type canonicalDeleteIndex struct {
	engine.DocEngine
	failure    error
	calls      int
	tenant, kb string
}

func (f *canonicalDeleteIndex) DeleteChunks(ctx context.Context, condition map[string]interface{}, base, kb string) (int64, error) {
	f.calls++
	if ctx == nil || base != "multirag_"+f.tenant || kb != f.kb || condition["doc_id"] == nil {
		return 0, errors.New("document delete escaped original scope")
	}
	return 1, f.failure
}

type canonicalDeleteStore struct {
	storage.Storage
	failure  error
	failOnce bool
}

func (s *canonicalDeleteStore) Remove(bucket, key string, tenant ...string) error {
	if err := s.Storage.Remove(bucket, key, tenant...); err != nil {
		return err
	}
	if s.failOnce {
		s.failOnce = false
		return s.failure
	}
	return nil
}
func canonicalLinkedDocs(t *testing.T, f *canonicalLinkFixture, kb string) []entity.Document {
	t.Helper()
	var docs []entity.Document
	if err := f.DB.Where("kb_id=?", kb).Order("id").Find(&docs).Error; err != nil {
		t.Fatal(err)
	}
	return docs
}

func TestDocumentDeleteLiveRejectsBeforeSideEffects(t *testing.T) {
	f := canonicalLink(t)
	if err := f.service.convertFiles([]string{f.one}, []string{f.a}, f.owner); err != nil {
		t.Fatal(err)
	}
	if err := f.service.convertFiles([]string{f.two}, []string{f.b}, f.owner); err != nil {
		t.Fatal(err)
	}
	a, b := canonicalLinkedDocs(t, f, f.a)[0], canonicalLinkedDocs(t, f, f.b)[0]
	index := &canonicalDeleteIndex{tenant: f.owner, kb: f.a}
	s := f.service.documentSvc
	s.docEngine = index
	s.metadataSvc = nil
	for _, request := range []struct {
		actor string
		ids   []string
	}{{f.other, []string{a.ID}}, {f.owner, []string{a.ID, b.ID}}, {f.owner, []string{a.ID, "missing"}}} {
		if count, err := s.DeleteDocuments(request.ids, false, f.a, request.actor); err == nil || count != 0 {
			t.Fatal("invalid batch accepted", count, err)
		}
	}
	if index.calls != 0 {
		t.Fatal("invalid batch reached document engine")
	}
	f.counts(t, f.a, 1)
	f.counts(t, f.b, 1)
	for _, id := range []string{f.one, f.two} {
		f.ReadObject(t, f.folder, id+".txt", []byte("abc"))
	}
}
func TestDocumentDeleteLiveFinalSQLAndObjectFailureCompensate(t *testing.T) {
	for _, scenario := range []string{"index", "counter-sql", "object-after-remove"} {
		t.Run(scenario, func(t *testing.T) {
			f := canonicalLink(t)
			if err := f.service.convertFiles([]string{f.one}, []string{f.a}, f.owner); err != nil {
				t.Fatal(err)
			}
			doc := canonicalLinkedDocs(t, f, f.a)[0]
			taskID := f.Prefix + "task"
			if err := f.DB.Create(&entity.Task{ID: taskID, DocID: doc.ID}).Error; err != nil {
				t.Fatal(err)
			}
			first := errors.New("controlled document cleanup failure")
			index := &canonicalDeleteIndex{tenant: f.owner, kb: f.a}
			s := f.service.documentSvc
			s.docEngine = index
			s.metadataSvc = nil
			switch scenario {
			case "index":
				index.failure = first
			case "counter-sql":
				if err := f.DB.Callback().Update().Before("gorm:update").Register("canonical-counter-failure", func(tx *gorm.DB) {
					if tx.Statement.Schema != nil && tx.Statement.Schema.Name == "Knowledgebase" {
						tx.AddError(first)
					}
				}); err != nil {
					t.Fatal(err)
				}
				t.Cleanup(func() { f.DB.Callback().Update().Remove("canonical-counter-failure") })
			case "object-after-remove":
				storage.GetStorageFactory().SetStorage(&canonicalDeleteStore{Storage: f.Store, failure: first, failOnce: true})
			}
			count, err := s.DeleteDocuments([]string{doc.ID}, false, f.a, f.owner)
			if count != 0 || !errors.Is(err, first) {
				t.Fatal("cleanup failure became success/lost its cause", count, err)
			}
			f.counts(t, f.a, 1)
			var tasks, mappings int
			if err := f.Read.QueryRow("SELECT count(*) FROM task WHERE id=?", taskID).Scan(&tasks); err != nil || tasks != 1 {
				t.Fatal("failed cleanup lost task row", tasks, err)
			}
			if err := f.Read.QueryRow("SELECT count(*) FROM file2document WHERE document_id=?", doc.ID).Scan(&mappings); err != nil || mappings != 1 {
				t.Fatal("failed cleanup lost source mapping", mappings, err)
			}
			f.ReadObject(t, f.folder, f.one+".txt", []byte("abc"))
			f.Journal.Event("canonical-document-cleanup-failure-readback", map[string]any{"scenario": scenario, "document_preserved": true, "task_preserved": true, "source_mapping_preserved": true, "object_restored_or_unchanged": true})
		})
	}
}
func TestDocumentDeleteLivePreservesSharedFileUntilLastReference(t *testing.T) {
	f := canonicalLink(t)
	if err := f.service.convertFiles([]string{f.one}, []string{f.a, f.b}, f.owner); err != nil {
		t.Fatal(err)
	}
	s := f.service.documentSvc
	a, b := canonicalLinkedDocs(t, f, f.a)[0], canonicalLinkedDocs(t, f, f.b)[0]
	if count, err := s.DeleteDocuments([]string{a.ID, a.ID}, false, f.a, f.owner); err != nil || count != 1 {
		t.Fatal(count, err)
	}
	f.counts(t, f.a, 0)
	f.counts(t, f.b, 1)
	f.ReadObject(t, f.folder, f.one+".txt", []byte("abc"))
	if count, err := s.DeleteDocuments([]string{b.ID}, false, f.b, f.owner); err != nil || count != 1 {
		t.Fatal(count, err)
	}
	f.counts(t, f.b, 0)
	var files, mappings int
	if err := f.Read.QueryRow("SELECT count(*) FROM file WHERE id=?", f.one).Scan(&files); err != nil || files != 0 {
		t.Fatal(files, err)
	}
	if err := f.Read.QueryRow("SELECT count(*) FROM file2document WHERE file_id=?", f.one).Scan(&mappings); err != nil || mappings != 0 {
		t.Fatal(mappings, err)
	}
	if keys := f.ObjectKeys(t); len(keys) != 1 || keys[0] != f.folder+"/"+f.two+".txt" {
		t.Fatal("last source was not removed or unrelated object removed", keys)
	}
}
