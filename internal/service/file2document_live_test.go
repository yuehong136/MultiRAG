package service

import (
	"errors"
	"github.com/redis/go-redis/v9"
	"reflect"
	"sync"
	"testing"

	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/testsupport/scratch"
)

func TestFileLinkScratchLifecycle(t *testing.T) {
	f := scratch.New(t)
	previous := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = previous })
	if err := f.DB.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.Knowledgebase{}, &entity.File{}, &entity.Document{}, &entity.File2Document{}, &entity.Task{}); err != nil {
		t.Fatal(err)
	}
	p := f.Prefix
	owner, other, member, invite := p+"owner", p+"other", p+"member", p+"invite"
	root, folder, one, two := p+"root", p+"folder", p+"one", p+"two"
	a, b := p+"a", p+"b"
	active := "1"
	location := "source.txt"
	for _, id := range []string{owner, other, member, invite} {
		if err := f.DB.Create(&entity.User{ID: id, Email: id + "@example.test", Nickname: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, id := range []string{owner, other} {
		if err := f.DB.Create(&entity.Tenant{ID: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, m := range []entity.UserTenant{{ID: p + "m", TenantID: owner, UserID: member, Role: "normal", Status: &active}, {ID: p + "i", TenantID: owner, UserID: invite, Role: "invite", Status: &active}} {
		if err := f.DB.Create(&m).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, id := range []string{a, b} {
		if err := f.DB.Create(&entity.Knowledgebase{ID: id, TenantID: owner, CreatedBy: owner, Name: id, EmbdID: "", ParserID: "naive", ParserConfig: entity.JSONMap{}, Permission: "team", Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, file := range []entity.File{{ID: root, ParentID: root, TenantID: owner, CreatedBy: owner, Name: "/", Type: "folder"}, {ID: folder, ParentID: root, TenantID: owner, CreatedBy: owner, Name: "nested", Type: "folder"}, {ID: one, ParentID: folder, TenantID: owner, CreatedBy: owner, Name: "one.txt", Type: "doc", SourceType: "local", Location: &location, Size: 3}, {ID: two, ParentID: folder, TenantID: owner, CreatedBy: owner, Name: "two.txt", Type: "doc", SourceType: "local", Location: &location, Size: 3}} {
		if err := f.DB.Create(&file).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := f.Store.Put(folder, location, []byte("abc")); err != nil {
		t.Fatal(err)
	}
	index := &deleteLiveIndex{datasets: map[string]bool{}, rows: map[string]map[string]any{}, metadata: map[string]string{}, expectedBase: map[string]string{}}
	s := &File2DocumentService{db: f.DB, objects: f.Store, deletion: NewDocumentDeleteService(f.DB, f.Redis, index, f.Store, server.EngineElasticsearch)}
	link := func(files, kbs []string, mode string) error {
		return s.LinkToDatasets(t.Context(), owner, LinkToDatasetsRequest{FileIDs: files, KBIDs: kbs}, mode)
	}
	state := func() []string {
		t.Helper()
		rows, err := f.Read.Query("SELECT id FROM t_ai_documents ORDER BY id")
		if err != nil {
			t.Fatal(err)
		}
		defer rows.Close()
		ids := []string{}
		for rows.Next() {
			var id string
			if rows.Scan(&id) != nil {
				t.Fatal("scan")
			}
			ids = append(ids, id)
		}
		if rows.Err() != nil {
			t.Fatal(rows.Err())
		}
		return ids
	}
	assertCount := func(kb string, want int) {
		t.Helper()
		var actual, count int
		if err := f.Read.QueryRow("SELECT doc_num,(SELECT count(*) FROM t_ai_documents WHERE kb_id=$1) FROM t_ai_knowledgebases WHERE id=$1", kb).Scan(&actual, &count); err != nil || actual != want || count != want {
			t.Fatalf("counter/readback got %d/%d want %d error=%v", actual, count, want, err)
		}
	}
	if err := link([]string{folder, one}, []string{a, a}, "replace"); err != nil {
		t.Fatal(err)
	}
	assertCount(a, 2)
	before := state()
	if err := link([]string{one, two}, []string{a}, "replace"); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(before, state()) {
		t.Fatal("selected docs were recreated")
	}
	for _, id := range before {
		result, err := (&DocumentFileService{db: f.DB, store: f.Store}).DownloadDocument(t.Context(), owner, a, id)
		if err != nil || string(result.Data) != "abc" {
			t.Fatal("actual converted document consumer", err)
		}
	}
	if err := link([]string{folder}, []string{b}, "add"); err != nil {
		t.Fatal(err)
	}
	assertCount(a, 2)
	assertCount(b, 2)
	taskID, queue := p+"task", p+"queue"
	f.Journal.Event("reserve-link-task-queue", map[string]any{"task": taskID, "document": before[0], "queue": queue, "cancel_key": taskID + "-cancel"})
	if err := f.DB.Create(&entity.Task{ID: taskID, DocID: before[0], Progress: 0.2}).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Model(&entity.Document{}).Where("id=?", before[0]).Update("run", "1").Error; err != nil {
		t.Fatal(err)
	}
	if err := f.Redis.XAdd(t.Context(), &redis.XAddArgs{Stream: queue, Values: map[string]any{"id": taskID, "doc_id": before[0]}}).Err(); err != nil {
		t.Fatal(err)
	}
	if err := link([]string{folder}, []string{b}, "replace"); err != nil {
		t.Fatal(err)
	}
	assertCount(a, 0)
	assertCount(b, 2)
	if count, err := f.Redis.XLen(t.Context(), queue).Result(); err != nil || count != 1 {
		t.Fatal("original queue envelope not preserved", err)
	}
	if marker, err := f.Redis.Get(t.Context(), taskID+"-cancel").Result(); err != nil || marker == "" {
		t.Fatal("queued producer not fenced", err)
	}
	var remainingTasks int
	if err := f.Read.QueryRow("SELECT count(*) FROM t_ai_tasks WHERE id=$1", taskID).Scan(&remainingTasks); err != nil || remainingTasks != 0 {
		t.Fatal("task SQL retirement", err)
	}
	f.ReadObject(t, folder, location, []byte("abc"))
	// A store failure must leave the old rows/counters for retry. Any additions
	// already committed are retained and deduplicated on that retry.
	index.datasets[b] = true
	index.failDelete = true
	if err := link([]string{folder}, []string{a}, "replace"); err == nil {
		t.Fatal("failed retirement acknowledged success")
	}
	assertCount(a, 2)
	assertCount(b, 2)
	index.failDelete = false
	if err := link([]string{folder}, []string{a}, "replace"); err != nil {
		t.Fatal(err)
	}
	assertCount(a, 2)
	assertCount(b, 0)
	// Authorization and ancestry failures precede object access or writes.
	for _, actor := range []string{other, invite} {
		before := state()
		calls := f.Store.Calls
		err := s.LinkToDatasets(t.Context(), actor, LinkToDatasetsRequest{FileIDs: []string{one}, KBIDs: []string{b}}, "add")
		if err == nil || !reflect.DeepEqual(before, state()) || calls != f.Store.Calls {
			t.Fatal("unauthorized conversion side effect", err)
		}
	}
	foreignRoot := p + "foreignroot"
	if err := f.DB.Create(&entity.File{ID: foreignRoot, ParentID: foreignRoot, TenantID: other, CreatedBy: other, Name: "foreign", Type: "folder"}).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Model(&entity.File{}).Where("id=?", one).Update("parent_id", foreignRoot).Error; err != nil {
		t.Fatal(err)
	}
	if err := link([]string{one}, []string{b}, "add"); !errors.Is(err, ErrLinkUnavailable) {
		t.Fatal("forged parent accepted", err)
	}
	f.DB.Model(&entity.File{}).Where("id=?", one).Update("parent_id", folder)
	// A real SQL failure rolls back both document and dataset count.
	if err := f.DB.Exec(`CREATE FUNCTION reject_g_link() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'controlled mapping failure'; END $$; CREATE TRIGGER reject_g_link BEFORE INSERT ON t_ai_file2documents FOR EACH ROW EXECUTE FUNCTION reject_g_link()`).Error; err != nil {
		t.Fatal(err)
	}
	before = state()
	if err := link([]string{one}, []string{b}, "add"); err == nil {
		t.Fatal("mapping SQL failure succeeded")
	}
	if !reflect.DeepEqual(before, state()) {
		t.Fatal("partial SQL insertion")
	}
	assertCount(b, 0)
	if err := f.DB.Exec("DROP TRIGGER reject_g_link ON t_ai_file2documents; DROP FUNCTION reject_g_link()").Error; err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for range 2 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_ = s.LinkToDatasets(t.Context(), owner, LinkToDatasetsRequest{FileIDs: []string{one}, KBIDs: []string{b}}, "add")
		}()
	}
	wg.Wait()
	assertCount(b, 1)
	if err := link([]string{folder}, []string{}, "replace"); err != nil {
		t.Fatal(err)
	}
	assertCount(a, 0)
	assertCount(b, 0)
	f.ReadObject(t, folder, location, []byte("abc"))
	// Dataset-origin files require actual copied bytes and compensation of an
	// acknowledged write failure, then remain readable after all links clear.
	sourceFile, sourceDoc := p+"kbfile", p+"kbdoc"
	sourceLocation := "original.txt"
	name := "original.txt"
	if err := f.DB.Create(&entity.File{ID: sourceFile, ParentID: folder, TenantID: owner, CreatedBy: owner, Name: name, Type: "doc", SourceType: "knowledgebase", Location: &sourceLocation, Size: 3}).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Create(&entity.Document{ID: sourceDoc, KbID: a, CreatedBy: owner, Name: &name, Type: "doc", ParserID: "naive", ParserConfig: entity.JSONMap{}, Suffix: "txt", Location: &sourceLocation, Status: &active, Size: 3}).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Create(&entity.File2Document{ID: p + "kblink", FileID: &sourceFile, DocumentID: &sourceDoc}).Error; err != nil {
		t.Fatal(err)
	}
	f.DB.Model(&entity.Knowledgebase{}).Where("id=?", a).Update("doc_num", 1)
	if err := f.Store.Put(a, sourceLocation, []byte("xyz")); err != nil {
		t.Fatal(err)
	}
	keys := f.ObjectKeys(t)
	f.Store.PutFailure = true
	if err := link([]string{sourceFile}, []string{b}, "add"); err == nil {
		t.Fatal("persisted failed copy acknowledged")
	}
	f.Store.PutFailure = false
	if !reflect.DeepEqual(keys, f.ObjectKeys(t)) {
		t.Fatal("unconfirmed copy compensation")
	}
	assertCount(b, 0)
	if err := link([]string{sourceFile}, []string{b}, "replace"); err != nil {
		t.Fatal(err)
	}
	assertCount(a, 0)
	assertCount(b, 1)
	var copiedID, copiedLocation string
	if err := f.Read.QueryRow("SELECT id,location FROM t_ai_documents WHERE kb_id=$1", b).Scan(&copiedID, &copiedLocation); err != nil {
		t.Fatal(err)
	}
	f.ReadObject(t, b, copiedLocation, []byte("xyz"))
	if err := link([]string{sourceFile}, []string{}, "replace"); err != nil {
		t.Fatal(err)
	}
	assertCount(b, 0)
	var sourceType, localLocation string
	if err := f.Read.QueryRow("SELECT source_type,location FROM t_ai_files WHERE id=$1", sourceFile).Scan(&sourceType, &localLocation); err != nil || sourceType != "local" {
		t.Fatal("last reference source not retained", err)
	}
	f.ReadObject(t, folder, localLocation, []byte("xyz"))
	if err := link([]string{sourceFile}, []string{a}, "add"); err != nil {
		t.Fatal("detached source cannot relink", err)
	}
	assertCount(a, 1)
}

func TestFileLinkArguments(t *testing.T) {
	for _, test := range []struct {
		req  LinkToDatasetsRequest
		mode string
	}{{LinkToDatasetsRequest{}, "replace"}, {LinkToDatasetsRequest{FileIDs: []string{}, KBIDs: []string{}}, "add"}, {LinkToDatasetsRequest{FileIDs: []string{"f"}, KBIDs: []string{"k"}}, "unknown"}, {LinkToDatasetsRequest{FileIDs: []string{"../x"}, KBIDs: []string{"k"}}, "add"}} {
		if err := NewFile2DocumentService().LinkToDatasets(t.Context(), "actor", test.req, test.mode); !errors.Is(err, ErrLinkArgument) {
			t.Fatal(err)
		}
	}
}
