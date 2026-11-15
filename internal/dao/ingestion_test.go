package dao

import (
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"

	"github.com/DATA-DOG/go-sqlmock"
	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/testsupport/scratch"
)

func TestIngestionTransactionFailures(t *testing.T) {
	for _, operation := range []string{"create-query", "create-commit", "running-commit", "stopping-commit", "remove-log-query", "remove-commit"} {
		t.Run(operation, func(t *testing.T) {
			sqlDB, mock, err := sqlmock.New()
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { sqlDB.Close() })
			db, err := gorm.Open(mysql.New(mysql.Config{Conn: sqlDB, SkipInitializeWithVersion: true}), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent), DisableAutomaticPing: true})
			if err != nil {
				t.Fatal(err)
			}
			old := DB
			DB = db
			t.Cleanup(func() { DB = old })
			fault := errors.New("controlled transaction fault")
			mock.ExpectBegin()
			status := common.RUNNING
			switch operation {
			case "create-query":
				mock.ExpectQuery("SELECT .*ingestion_task.*FOR UPDATE").WillReturnError(fault)
				mock.ExpectRollback()
			default:
				switch operation {
				case "create-commit":
					status = common.FAILED
				case "running-commit":
					status = common.CREATED
				case "stopping-commit":
					status = common.RUNNING
				case "remove-log-query", "remove-commit":
					status = common.STOPPED
				}
				mock.ExpectQuery("SELECT .*ingestion_task.*FOR UPDATE").WillReturnRows(sqlmock.NewRows([]string{"id", "document_id", "user_id", "status"}).AddRow("task", "doc", "owner", status))
				if strings.HasPrefix(operation, "remove-") {
					mock.ExpectQuery("SELECT .*ingestion_tasklet.*").WillReturnRows(sqlmock.NewRows([]string{"id", "task_id"}).AddRow("step", "task"))
					if operation == "remove-log-query" {
						mock.ExpectQuery("SELECT .*ingestion_tasklet_log.*").WillReturnError(fault)
						mock.ExpectRollback()
					} else {
						mock.ExpectQuery("SELECT .*ingestion_tasklet_log.*").WillReturnRows(sqlmock.NewRows([]string{"id", "tasklet_id"}))
						mock.ExpectQuery("SELECT .*ingestion_task_log.*").WillReturnRows(sqlmock.NewRows([]string{"id", "task_id"}))
						mock.ExpectExec("DELETE FROM .*ingestion_task.*").WillReturnResult(sqlmock.NewResult(0, 1))
						mock.ExpectCommit().WillReturnError(fault)
					}
				} else {
					mock.ExpectExec("UPDATE .*ingestion_task.*").WillReturnResult(sqlmock.NewResult(0, 1))
					mock.ExpectCommit().WillReturnError(fault)
				}
			}
			d := NewIngestionTaskDAO()
			var got *entity.IngestionTask
			switch operation {
			case "create-query", "create-commit":
				got, err = d.CheckAndCreate(&entity.IngestionTask{DocumentID: "doc"})
			case "running-commit":
				got, err = d.SetRunningByIngestor("task")
			case "stopping-commit":
				got, err = d.SetStoppingByAPIServer("task")
			default:
				var info *TaskInfo
				info, err = d.RemoveByAPIServerOrAdminServer("task", nil)
				if info != nil {
					t.Fatal("uncommitted removal returned success")
				}
			}
			if !errors.Is(err, fault) || got != nil {
				t.Fatalf("failure lost: result=%v err=%v", got, err)
			}
			if err := mock.ExpectationsWereMet(); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestIngestionMySQLAtomicTransitions(t *testing.T) {
	f := scratch.New(t)
	old := DB
	DB = f.DB
	t.Cleanup(func() { DB = old })
	if err := DB.AutoMigrate(&entity.IngestionTask{}, &entity.IngestionTaskLog{}, &entity.IngestionTasklet{}, &entity.IngestionTaskletLog{}); err != nil {
		t.Fatal(err)
	}
	d := NewIngestionTaskDAO()
	task := &entity.IngestionTask{ID: "task", UserID: "owner", DocumentID: "doc", DatasetID: "kb", Status: common.FAILED}
	if err := DB.Create(task).Error; err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	results := make(chan error, 12)
	for range 12 {
		wg.Go(func() { _, err := d.CheckAndCreate(&entity.IngestionTask{DocumentID: "doc"}); results <- err })
	}
	wg.Wait()
	close(results)
	succeeded := 0
	for err := range results {
		if err == nil {
			succeeded++
		} else if !strings.Contains(err.Error(), "already exists") {
			t.Fatal(err)
		}
	}
	if succeeded != 1 {
		t.Fatalf("retry committed %d times", succeeded)
	}
	var status string
	read := func(want string) {
		t.Helper()
		if err := f.Read.QueryRow("SELECT status FROM ingestion_task WHERE id=?", "task").Scan(&status); err != nil || status != want {
			t.Fatalf("status=%s want=%s err=%v", status, want, err)
		}
	}
	read(common.CREATED)
	results = make(chan error, 16)
	for range 8 {
		wg.Go(func() { _, err := d.SetRunningByIngestor("task"); results <- err })
		wg.Go(func() { _, err := d.SetStoppingByAPIServer("task"); results <- err })
	}
	wg.Wait()
	close(results)
	for err := range results {
		if err != nil {
			t.Fatal(err)
		}
	}
	read(common.STOPPED)
	if err := DB.Model(task).Update("status", common.RUNNING).Error; err != nil {
		t.Fatal(err)
	}
	row, err := d.SetRunningByIngestor(task.ID)
	if err != nil || row.Status != common.RUNNING {
		t.Fatalf("redelivery running changed: %v %v", row, err)
	}
	read(common.RUNNING)
	if err := DB.Exec("CREATE TRIGGER fail_ingestion BEFORE UPDATE ON ingestion_task FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='controlled transition failure'").Error; err != nil {
		t.Fatal(err)
	}
	if row, err := d.SetStoppingByAPIServer(task.ID); err == nil || row != nil {
		t.Fatal("failed transition accepted")
	}
	read(common.RUNNING)
	if err := DB.Exec("DROP TRIGGER fail_ingestion").Error; err != nil {
		t.Fatal(err)
	}
	if _, err := d.SetStoppingByAPIServer(task.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := d.SetRunningByIngestor(task.ID); err != nil {
		t.Fatal(err)
	}
	read(common.STOPPED)
	for _, record := range []any{&entity.IngestionTaskLog{ID: 1, TaskID: "task", Checkpoint: entity.JSONMap{"files": []string{"a", "a", "b"}}}, &entity.IngestionTasklet{ID: "step", TaskID: "task"}, &entity.IngestionTaskletLog{ID: 1, TaskletID: "step", Checkpoint: entity.JSONMap{"files": []string{"c"}}}} {
		if err := DB.Create(record).Error; err != nil {
			t.Fatal(err)
		}
	}
	foreign := "foreign"
	if _, err := d.RemoveByAPIServerOrAdminServer(task.ID, &foreign); err == nil {
		t.Fatal("foreign removal accepted")
	}
	read(common.STOPPED)
	info, err := d.RemoveByAPIServerOrAdminServer(task.ID, nil)
	if err != nil || len(info.FilesToDelete) != 2 || len(info.Tasklets) != 1 || len(info.Tasklets[0].FilesToDelete) != 1 {
		t.Fatalf("persisted checkpoint plan lost: %+v %v", info, err)
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM ingestion_task WHERE id=?", "task").Scan(&count); err != nil || count != 0 {
		t.Fatal(fmt.Sprint("removal not committed ", err))
	}
}
