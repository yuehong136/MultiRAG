package service

import (
	"context"
	"errors"
	"github.com/redis/go-redis/v9"
	"reflect"
	"slices"
	"sync"
	"testing"
	"time"

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
	if slices.Contains(f.ObjectKeys(t), a+"/"+sourceLocation) {
		t.Fatal("retired original dataset object leaked")
	}
	var copiedID, copiedLocation string
	if err := f.Read.QueryRow("SELECT id,location FROM t_ai_documents WHERE kb_id=$1", b).Scan(&copiedID, &copiedLocation); err != nil {
		t.Fatal(err)
	}
	f.ReadObject(t, b, copiedLocation, []byte("xyz"))
	if err := link([]string{sourceFile}, []string{}, "replace"); err != nil {
		t.Fatal(err)
	}
	assertCount(b, 0)
	if slices.Contains(f.ObjectKeys(t), b+"/"+copiedLocation) {
		t.Fatal("retired copied dataset object leaked")
	}
	var sourceType, localLocation string
	if err := f.Read.QueryRow("SELECT source_type,location FROM t_ai_files WHERE id=$1", sourceFile).Scan(&sourceType, &localLocation); err != nil || sourceType != "local" {
		t.Fatal("last reference source not retained", err)
	}
	f.ReadObject(t, folder, localLocation, []byte("xyz"))
	if err := link([]string{sourceFile}, []string{a}, "add"); err != nil {
		t.Fatal("detached source cannot relink", err)
	}
	assertCount(a, 1)
	// Cross-tenant targets are rejected before any SQL/object mutation, even
	// when a valid admin membership grants the actor access to the other team.
	foreignKB := p + "foreignkb"
	if err := f.DB.Create(&entity.UserTenant{ID: p + "crossmember", UserID: owner, TenantID: other, Role: "admin", Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Create(&entity.Knowledgebase{ID: foreignKB, TenantID: other, CreatedBy: other, Name: foreignKB, EmbdID: "", ParserID: "naive", ParserConfig: entity.JSONMap{}, Permission: "team", Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	before = state()
	calls := f.Store.Calls
	objectKeys := f.ObjectKeys(t)
	if err := link([]string{one}, []string{a, foreignKB}, "add"); !errors.Is(err, ErrLinkUnavailable) {
		t.Fatal("cross-tenant source registration accepted", err)
	}
	if !reflect.DeepEqual(before, state()) || calls != f.Store.Calls || !reflect.DeepEqual(objectKeys, f.ObjectKeys(t)) {
		t.Fatal("cross-tenant rejection had side effects")
	}
	assertCount(foreignKB, 0)
	// A genuinely shared bucket/key survives until its final independent SQL
	// reference is retired, while mappings to differently addressed copies do
	// not preserve the old source object.
	sharedFile, sharedDoc, keepDoc := p+"sharedfile", p+"shareddoc", p+"keepdoc"
	sharedLocation := "shared.txt"
	if err := f.DB.Create(&entity.File{ID: sharedFile, ParentID: folder, TenantID: owner, CreatedBy: owner, Name: sharedLocation, Type: "doc", SourceType: "knowledgebase", Location: &sharedLocation, Size: 3}).Error; err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{sharedDoc, keepDoc} {
		if err := f.DB.Create(&entity.Document{ID: id, KbID: a, CreatedBy: owner, Name: &sharedLocation, Type: "doc", ParserID: "naive", ParserConfig: entity.JSONMap{}, Suffix: "txt", Location: &sharedLocation, Status: &active, Size: 3}).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := f.DB.Create(&entity.File2Document{ID: p + "sharedlink", FileID: &sharedFile, DocumentID: &sharedDoc}).Error; err != nil {
		t.Fatal(err)
	}
	f.DB.Model(&entity.Knowledgebase{}).Where("id=?", a).Update("doc_num", 3)
	if err := f.Store.Put(a, sharedLocation, []byte("def")); err != nil {
		t.Fatal(err)
	}
	if err := link([]string{sharedFile}, []string{b}, "replace"); err != nil {
		t.Fatal(err)
	}
	f.ReadObject(t, a, sharedLocation, []byte("def"))
	if _, err := s.deletion.DeleteDocuments(t.Context(), a, owner, []string{keepDoc}, false); err != nil {
		t.Fatal(err)
	}
	if slices.Contains(f.ObjectKeys(t), a+"/"+sharedLocation) {
		t.Fatal("last shared source reference leaked")
	}
}

type pausedFileRetirer struct {
	inner            *DocumentDeleteService
	entered, release chan struct{}
	once             sync.Once
}

func (s *pausedFileRetirer) DeleteDocuments(ctx context.Context, kb, actor string, ids []string, all bool) (int, error) {
	s.once.Do(func() { close(s.entered); <-s.release })
	return s.inner.DeleteDocuments(ctx, kb, actor, ids, all)
}

func TestFileLinkReplaceSerializesRetirement(t *testing.T) {
	f := scratch.New(t)
	previous := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = previous })
	if err := f.DB.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.Knowledgebase{}, &entity.File{}, &entity.Document{}, &entity.File2Document{}, &entity.Task{}); err != nil {
		t.Fatal(err)
	}
	p := f.Prefix
	actor, root, file, a, b := p+"owner", p+"root", p+"file", p+"a", p+"b"
	active := "1"
	location := "source.txt"
	for _, row := range []any{&entity.User{ID: actor, Email: actor + "@example.test", Nickname: "actor", Status: &active}, &entity.Tenant{ID: actor, Status: &active}, &entity.File{ID: root, ParentID: root, TenantID: actor, CreatedBy: actor, Name: "/", Type: "folder"}, &entity.File{ID: file, ParentID: root, TenantID: actor, CreatedBy: actor, Name: location, Type: "doc", Location: &location, SourceType: "local", Size: 3}} {
		if err := f.DB.Create(row).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, id := range []string{a, b} {
		if err := f.DB.Create(&entity.Knowledgebase{ID: id, TenantID: actor, CreatedBy: actor, Name: id, EmbdID: "", ParserID: "naive", ParserConfig: entity.JSONMap{}, Permission: "me", Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := f.Store.Put(root, location, []byte("abc")); err != nil {
		t.Fatal(err)
	}
	index := &deleteLiveIndex{datasets: map[string]bool{}, rows: map[string]map[string]any{}, metadata: map[string]string{}, expectedBase: map[string]string{}}
	deleter := NewDocumentDeleteService(f.DB, f.Redis, index, f.Store, server.EngineElasticsearch)
	s := &File2DocumentService{db: f.DB, objects: f.Store, deletion: deleter}
	request := func(kb string) LinkToDatasetsRequest {
		return LinkToDatasetsRequest{FileIDs: []string{file}, KBIDs: []string{kb}}
	}
	if err := s.LinkToDatasets(t.Context(), actor, request(a), "replace"); err != nil {
		t.Fatal(err)
	}
	barrier := &pausedFileRetirer{inner: deleter, entered: make(chan struct{}), release: make(chan struct{})}
	first := *s
	first.deletion = barrier
	var once sync.Once
	release := func() { once.Do(func() { close(barrier.release) }) }
	defer release()
	done1, done2 := make(chan error, 1), make(chan error, 1)
	go func() { done1 <- first.LinkToDatasets(t.Context(), actor, request(b), "replace") }()
	select {
	case <-barrier.entered:
	case <-time.After(3 * time.Second):
		t.Fatal("first replace did not reach pre-retirement barrier")
	}
	go func() { done2 <- s.LinkToDatasets(t.Context(), actor, request(a), "replace") }()
	// Wait for evidence from PostgreSQL that R2 is blocked on the stable
	// advisory intent, while R1 has already committed B and has not retired A.
	deadline := time.Now().Add(3 * time.Second)
	waiting := false
	for time.Now().Before(deadline) {
		var count int
		err := f.Read.QueryRow("SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid WHERE a.datname=$1 AND l.locktype='advisory' AND NOT l.granted", f.Database).Scan(&count)
		if err != nil {
			t.Fatal(err)
		}
		if count > 0 {
			waiting = true
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !waiting {
		release()
		t.Fatal("second replace was not serialized before retirement")
	}
	release()
	for _, done := range []chan error{done1, done2} {
		select {
		case err := <-done:
			if err != nil {
				t.Fatal(err)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("serialized replace did not complete")
		}
	}
	var count int
	var finalKB string
	if err := f.Read.QueryRow("SELECT count(*),min(d.kb_id) FROM t_ai_file2documents f JOIN t_ai_documents d ON d.id=f.document_id WHERE f.file_id=$1", file).Scan(&count, &finalKB); err != nil || count != 1 || finalKB != a {
		t.Fatal("non-serial replace result", count, finalKB, err)
	}
	f.ReadObject(t, root, location, []byte("abc"))
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
