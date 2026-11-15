package handler

import (
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/service"
	"sync"
)

func TestDatasetTimeHTTPAndSQL(t *testing.T) {
	f := newCanonicalHTTPFixture(t)
	svc := service.NewDatasetService()
	h := NewDatasetsHandler(svc, nil)
	app := f.app()
	app.POST("/datasets", h.CreateDataset)
	app.GET("/datasets/:dataset_id", h.GetDataset)
	app.POST("/kb/update", NewKnowledgebaseHandler(service.NewKnowledgebaseService(), service.NewUserService(), service.NewDocumentService()).UpdateKB)
	url := f.serve(t, app)
	before := time.Now()
	status, result, _, _ := f.request(t, url, "owner", "POST", "/datasets", map[string]any{"name": "canonical created"})
	if status != 200 || result["code"] != float64(0) {
		t.Fatal("create dataset", result)
	}
	data := result["data"].(map[string]any)
	id := data["id"].(string)
	parsed, err := uuid.Parse(id)
	if err != nil || parsed.Version() != 1 || len(id) != 32 || strings.Contains(id, "-") {
		t.Fatal("created dataset ID", id, err)
	}
	var create, update int64
	var created, updated time.Time
	var count int64
	read := func() {
		t.Helper()
		if err := f.Read.QueryRow("SELECT create_time,update_time,create_date,update_date,doc_num FROM knowledgebase WHERE id=?", id).Scan(&create, &update, &created, &updated, &count); err != nil {
			t.Fatal(err)
		}
	}
	read()
	if create < before.UnixMilli() || create > time.Now().UnixMilli() || created.Unix() != create/1000 || updated.Unix() != update/1000 || created.Nanosecond() != 0 || count != 0 {
		t.Fatal("millisecond/date/zero SQL contract", create, created, count)
	}
	_, got, _, _ := f.request(t, url, "owner", "GET", "/datasets/"+id, nil)
	if got["code"] != float64(0) || got["data"].(map[string]any)["id"] != id {
		t.Fatal("created dataset cannot be read", got)
	}
	originalCreate := create
	_, changed, _, _ := f.request(t, url, "owner", "POST", "/kb/update", map[string]any{"kb_id": id, "parser_id": "naive", "name": "renamed"})
	if changed["code"] != float64(common.CodeSuccess) {
		t.Fatal("update dataset", changed)
	}
	read()
	if create != originalCreate || update < create {
		t.Fatal("update corrupted creation", create, update)
	}
	_, denied, _, _ := f.request(t, url, "foreign", "POST", "/kb/update", map[string]any{"kb_id": id, "parser_id": "naive", "name": "forbidden"})
	if denied["code"] == float64(0) {
		t.Fatal("foreign update accepted", denied)
	}
	var name string
	if err := f.Read.QueryRow("SELECT name FROM knowledgebase WHERE id=?", id).Scan(&name); err != nil || name != "renamed" {
		t.Fatal("denied write mutated SQL", name, err)
	}
	// Real concurrent original counter updates must remain atomic and keep legal zero.
	kbDAO := dao.NewKnowledgebaseDAO()
	var wg sync.WaitGroup
	failures := make(chan error, 16)
	for range 16 {
		wg.Add(1)
		go func() { defer wg.Done(); failures <- kbDAO.AtomicIncreaseDocNumByID(id) }()
	}
	wg.Wait()
	close(failures)
	for err := range failures {
		if err != nil {
			t.Fatal(err)
		}
	}
	read()
	if count != 16 {
		t.Fatal("atomic increments lost", count)
	}
	if err := kbDAO.DecreaseDocumentNum(id, 16, 0, 0); err != nil {
		t.Fatal(err)
	}
	read()
	if count != 0 || create != originalCreate {
		t.Fatal("legal zero or creation changed", count, create)
	}
	trigger := "reject_dataset_update"
	f.Journal.Event("reserve-mysql-trigger", map[string]any{"database": f.Database, "trigger": trigger})
	if err := f.DB.Exec("CREATE TRIGGER " + trigger + " BEFORE UPDATE ON knowledgebase FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='controlled dataset rejection'").Error; err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := f.DB.Exec("DROP TRIGGER IF EXISTS " + trigger).Error; err != nil {
			t.Error(err)
		}
		var remaining int
		if err := f.Read.QueryRow("SELECT count(*) FROM information_schema.triggers WHERE trigger_schema=? AND trigger_name=?", f.Database, trigger).Scan(&remaining); err != nil || remaining != 0 {
			t.Error("trigger cleanup readback", remaining, err)
		}
	})
	_, failed, _, _ := f.request(t, url, "owner", "POST", "/kb/update", map[string]any{"kb_id": id, "parser_id": "naive", "name": "must not persist"})
	if failed["code"] != float64(common.CodeServerError) {
		t.Fatal("SQL update failure became success", failed)
	}
	if err := kbDAO.AtomicIncreaseDocNumByID(id); err == nil {
		t.Fatal("counter SQL failure swallowed")
	}
	read()
	if count != 0 || create != originalCreate {
		t.Fatal("failed counter mutated SQL", count, create)
	}
	if err := f.Read.QueryRow("SELECT name FROM knowledgebase WHERE id=?", id).Scan(&name); err != nil || name != "renamed" {
		t.Fatal("failed update persisted", name, err)
	}

	f.Journal.Event("canonical-dataset-time-readback", map[string]any{"create_get_update_http_mysql": true, "uuid1_still_required": true, "epoch_milliseconds": true, "dates_second_resolution": true, "foreign_update_zero_writes": true})
}
