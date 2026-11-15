package service

import (
	"errors"
	"gorm.io/gorm"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/storage"
	"multirag/internal/testsupport/scratch"
	"reflect"
	"testing"
	"time"
)

type canonicalLinkFixture struct {
	*scratch.Services
	service                                              *File2DocumentService
	owner, other, member, invite, folder, one, two, a, b string
}

func canonicalLink(t *testing.T) *canonicalLinkFixture {
	t.Helper()
	canonicalServiceConfig(t)
	f := &canonicalLinkFixture{Services: scratch.New(t)}
	oldDB, oldStore := dao.DB, storage.GetStorageFactory().GetStorage()
	dao.DB = f.DB
	storage.GetStorageFactory().SetStorage(f.Store)
	t.Cleanup(func() { dao.DB = oldDB; storage.GetStorageFactory().SetStorage(oldStore) })
	if err := f.DB.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.Knowledgebase{}, &entity.File{}, &entity.Document{}, &entity.File2Document{}, &entity.Task{}); err != nil {
		t.Fatal(err)
	}
	f.owner, f.other, f.member, f.invite = f.Prefix+"owner", f.Prefix+"other", f.Prefix+"member", f.Prefix+"invite"
	f.folder, f.one, f.two, f.a, f.b = f.Prefix+"folder", f.Prefix+"one", f.Prefix+"two", f.Prefix+"a", f.Prefix+"b"
	active := "1"
	for _, id := range []string{f.owner, f.other, f.member, f.invite} {
		if err := f.DB.Create(&entity.User{ID: id, Email: id + "@fixture.invalid", Nickname: id, IsActive: "1", Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, id := range []string{f.owner, f.other} {
		if err := f.DB.Create(&entity.Tenant{ID: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, relation := range []entity.UserTenant{{ID: f.member, UserID: f.member, TenantID: f.owner, Role: "normal", Status: &active}, {ID: f.invite, UserID: f.invite, TenantID: f.owner, Role: "invite", Status: &active}} {
		if err := f.DB.Create(&relation).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, id := range []string{f.a, f.b} {
		if err := f.DB.Create(&entity.Knowledgebase{ID: id, TenantID: f.owner, CreatedBy: f.owner, Name: id, EmbdID: "", ParserID: "naive", ParserConfig: entity.JSONMap{}, Permission: "team", Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := f.DB.Create(&entity.File{ID: f.folder, ParentID: f.folder, TenantID: f.owner, CreatedBy: f.owner, Name: "/", Type: FileTypeFolder}).Error; err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{f.one, f.two} {
		location := id + ".txt"
		if err := f.DB.Create(&entity.File{ID: id, ParentID: f.folder, TenantID: f.owner, CreatedBy: f.owner, Name: id + ".txt", Type: "doc", SourceType: string(entity.FileSourceLocal), Location: &location, Size: 3}).Error; err != nil {
			t.Fatal(err)
		}
		if err := f.Store.Put(f.folder, location, []byte("abc")); err != nil {
			t.Fatal(err)
		}
	}
	f.service = NewFile2DocumentService()
	return f
}
func (f *canonicalLinkFixture) counts(t *testing.T, kb string, want int) {
	t.Helper()
	var rows, counter int
	if err := f.Read.QueryRow("SELECT doc_num,(SELECT count(*) FROM document WHERE kb_id=?) FROM knowledgebase WHERE id=?", kb, kb).Scan(&counter, &rows); err != nil || counter != want || rows != want {
		t.Fatal("SQL document/counter readback", counter, rows, want, err)
	}
}
func TestFileLinkScratchLifecycle(t *testing.T) {
	f := canonicalLink(t)
	for _, actor := range []string{f.other, f.invite} {
		if err := f.service.LinkToDatasets(actor, &LinkToDatasetsRequest{FileIDs: []string{f.one}, KbIDs: []string{f.a}}); !errors.Is(err, ErrLinkNoAuthorization) {
			t.Fatal("unauthorized file linking", actor, err)
		}
	}
	f.counts(t, f.a, 0)
	if err := f.service.LinkToDatasets(f.member, &LinkToDatasetsRequest{FileIDs: []string{f.folder, f.one}, KbIDs: []string{f.a, f.a}}); err != nil {
		t.Fatal(err)
	}
	deadline := time.NewTimer(5 * time.Second)
	defer deadline.Stop()
	tick := time.NewTicker(10 * time.Millisecond)
	defer tick.Stop()
	for {
		var count int
		if err := f.Read.QueryRow("SELECT count(*) FROM file2document").Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count == 2 {
			break
		}
		select {
		case <-deadline.C:
			t.Fatal("canonical asynchronous conversion did not complete")
		case <-tick.C:
		}
	}
	f.counts(t, f.a, 2)
	var docs []entity.Document
	if err := f.DB.Where("kb_id=?", f.a).Find(&docs).Error; err != nil {
		t.Fatal(err)
	}
	for _, doc := range docs {
		result, err := f.service.documentSvc.DownloadDocument(f.a, doc.ID)
		if err != nil || string(result.Data) != "abc" {
			t.Fatal("converted document byte consumer", err)
		}
	}
	if err := f.service.convertFiles([]string{f.one, f.two}, []string{f.b}, f.owner); err != nil {
		t.Fatal(err)
	}
	f.counts(t, f.a, 0)
	f.counts(t, f.b, 2)
	if keys := f.ObjectKeys(t); len(keys) != 2 {
		t.Fatal("linking modified source objects", keys)
	}
	for _, id := range []string{f.one, f.two} {
		f.ReadObject(t, f.folder, id+".txt", []byte("abc"))
	}
	f.Journal.Event("canonical-file-link-readback", map[string]any{"documents": 2, "sources_preserved": true, "old_dataset_count": 0, "target_dataset_count": 2, "asynchronous_target_contract": true})
}
func TestFileLinkMappingFailureCompensatesDocumentAndCounters(t *testing.T) {
	f := canonicalLink(t)
	failure := errors.New("controlled mapping create failure")
	if err := f.DB.Callback().Create().Before("gorm:create").Register("canonical-fail-link", func(tx *gorm.DB) {
		if tx.Statement.Schema != nil && tx.Statement.Schema.Name == "File2Document" {
			tx.AddError(failure)
		}
	}); err != nil {
		t.Fatal(err)
	}
	if err := f.service.convertFiles([]string{f.one}, []string{f.a}, f.owner); !errors.Is(err, failure) {
		t.Fatal("mapping failure became success", err)
	}
	f.counts(t, f.a, 0)
	var mappings int
	if err := f.Read.QueryRow("SELECT count(*) FROM file2document").Scan(&mappings); err != nil || mappings != 0 {
		t.Fatal(mappings, err)
	}
	f.ReadObject(t, f.folder, f.one+".txt", []byte("abc"))
}
func TestFileLinkArguments(t *testing.T) {
	f := canonicalLink(t)
	for _, request := range []*LinkToDatasetsRequest{nil, {}, {FileIDs: []string{f.one}}, {KbIDs: []string{f.a}}, {FileIDs: []string{f.one, "missing"}, KbIDs: []string{f.a}}} {
		if err := f.service.LinkToDatasets(f.owner, request); err == nil {
			t.Fatal("invalid complete selection accepted", request)
		}
	}
	f.counts(t, f.a, 0)
	if got := dedupeStrings([]string{"a", "a", "b"}); !reflect.DeepEqual(got, []string{"a", "b"}) {
		t.Fatal(got)
	}
}
