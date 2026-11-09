//go:build integration

package admin

import (
	"multirag/internal/common"
	"multirag/internal/entity"
	"testing"
)

func TestAdminTasksIntegration(t *testing.T) {
	f := newAdminFixture(t)
	if err := f.DB.Create(&entity.IngestionTask{ID: "task", UserID: "admin", DocumentID: "doc", DatasetID: "kb", Status: common.CREATED}).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Create(&entity.IngestionTaskLog{TaskID: "task", Checkpoint: entity.JSONMap{"current_step": 3}}).Error; err != nil {
		t.Fatal(err)
	}
	for _, actor := range []string{"", "normal", "inactive", "nil-admin"} {
		for _, method := range []string{"GET", "PUT", "DELETE"} {
			status, _ := f.request(t, actor, method, "/ingestion/tasks", `{"tasks":["task"]}`)
			if status != 401 && status != 403 {
				t.Fatal("unauthorized task route", actor, method, status)
			}
		}
	}
	var state string
	if err := f.Read.QueryRow("SELECT status FROM ingestion_task WHERE id='task'").Scan(&state); err != nil || state != common.CREATED {
		t.Fatal("denied task side effect", err)
	}
	status, result := f.request(t, "admin", "GET", "/ingestion/tasks", "")
	if status != 200 || result["code"] != float64(0) {
		t.Fatal(status, result)
	}
	rows := result["data"].([]any)
	if len(rows) != 1 || rows[0].(map[string]any)["step"] != float64(3) {
		t.Fatal("persisted checkpoint lost", rows)
	}
	status, result = f.request(t, "admin", "PUT", "/ingestion/tasks", `{"tasks":["task"]}`)
	if status != 200 || result["code"] != float64(0) {
		t.Fatal(status, result)
	}
	if err := f.Read.QueryRow("SELECT status FROM ingestion_task WHERE id='task'").Scan(&state); err != nil || state != common.STOPPED {
		t.Fatal("stop not committed", err)
	}
	status, result = f.request(t, "admin", "DELETE", "/ingestion/tasks", `{"tasks":["task"]}`)
	if status != 200 || result["code"] != float64(0) {
		t.Fatal(status, result)
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM ingestion_task WHERE id='task'").Scan(&count); err != nil || count != 0 {
		t.Fatal("remove not committed", err)
	}
	// An invalid persisted checkpoint returns one error response, with no panic or second success body.
	if err := f.DB.Create(&entity.IngestionTask{ID: "invalid", UserID: "admin", DocumentID: "doc", DatasetID: "kb", Status: common.CREATED}).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Create(&entity.IngestionTaskLog{TaskID: "invalid", Checkpoint: entity.JSONMap{"current_step": "invalid"}}).Error; err != nil {
		t.Fatal(err)
	}
	status, result = f.request(t, "admin", "GET", "/ingestion/tasks", "")
	if status < 400 || result["code"] == float64(0) {
		t.Fatal("corrupt checkpoint accepted", status, result)
	}
}
