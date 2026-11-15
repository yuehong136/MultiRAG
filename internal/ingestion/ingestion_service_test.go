package ingestion

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/testsupport/scratch"
)

func TestIngestionNATSTransportFailureRedeliveryAndLifecycle(t *testing.T) {
	f := scratch.New(t)
	lease := filepath.Join(filepath.Dir(os.Getenv("MULTIRAG_GO_ALIGNMENT_CONFIG")), "nats-acceptance.lease")
	f.Journal.Event("reserve-nats-lease", map[string]any{"path": lease, "pid": os.Getpid(), "stream": "RAGFLOW_TASKS", "consumer": "RAGFLOW_CONSUMER", "subject": "tasks.RAGFLOW"})
	lock, err := os.OpenFile(lease, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		t.Fatal("exclusive NATS fixture lease required")
	}
	fmt.Fprint(lock, os.Getpid())
	lock.Close()
	t.Cleanup(func() {
		if err := os.Remove(lease); err != nil {
			t.Error(err)
		}
	})
	if err := server.Init(filepath.Join(filepath.Dir(lease), "service_conf.yaml")); err != nil {
		t.Fatal("private config")
	}
	server.GetConfig().Nats = f.Config.Nats
	previous := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = previous })
	if err := f.DB.AutoMigrate(&entity.IngestionTask{}, &entity.IngestionTaskLog{}, &entity.IngestionTasklet{}, &entity.IngestionTaskletLog{}); err != nil {
		t.Fatal(err)
	}
	reader, err := nats.Connect("nats://" + net.JoinHostPort(f.Config.Nats.Host, fmt.Sprint(f.Config.Nats.Port)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(reader.Close)
	js, err := jetstream.New(reader)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := js.Stream(t.Context(), "RAGFLOW_TASKS"); !errors.Is(err, jetstream.ErrStreamNotFound) {
		t.Fatal("NATS fixture stream must initially be absent", err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := js.DeleteStream(ctx, "RAGFLOW_TASKS"); err != nil {
			t.Error(err)
		}
		_, err := js.Stream(ctx, "RAGFLOW_TASKS")
		f.Journal.Event("cleanup-nats-stream", map[string]any{"absent": errors.Is(err, jetstream.ErrStreamNotFound)})
		if !errors.Is(err, jetstream.ErrStreamNotFound) {
			t.Error("independent stream cleanup readback")
		}
		if err := engine.Close(); err != nil {
			t.Error(err)
		}
	})
	if err := engine.InitMessageQueueEngine("nats"); err != nil {
		t.Fatal(err)
	}
	queue := engine.GetMessageQueueEngine()
	if err := queue.InitConsumer("tasks.RAGFLOW"); err != nil {
		t.Fatal(err)
	}
	stream, err := js.Stream(t.Context(), "RAGFLOW_TASKS")
	if err != nil {
		t.Fatal(err)
	}
	publish := func(id string) {
		t.Helper()
		raw, _ := json.Marshal(common.TaskMessage{TaskID: id, TaskType: common.TaskTypeIngestionTask})
		if err := queue.PublishTask("tasks.RAGFLOW", raw); err != nil {
			t.Fatal(err)
		}
	}
	fetch := func() common.TaskHandle {
		t.Helper()
		rows, err := queue.GetMessages(1)
		if err != nil || len(rows) != 1 {
			t.Fatal("actual NATS fetch", len(rows), err)
		}
		return rows[0]
	}
	task := &entity.IngestionTask{ID: "fault-task", UserID: "owner", DocumentID: "fault-doc", DatasetID: "dataset", Status: common.CREATED, Schema: entity.JSONMap{"pipeline": []any{}}}
	if err := f.DB.Create(task).Error; err != nil {
		t.Fatal(err)
	}
	publish(task.ID)
	if messages, err := queue.ListMessages("", false); err != nil || len(messages) != 1 {
		t.Fatal("real queue inspection", messages, err)
	}
	first := fetch()
	running, err := dao.NewIngestionTaskDAO().SetRunningByIngestor(task.ID)
	if err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Exec("CREATE TRIGGER reject_ingestion_checkpoint BEFORE INSERT ON ingestion_task_log FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='controlled checkpoint failure'").Error; err != nil {
		t.Fatal(err)
	}
	worker := NewIngestor("acceptance", 1, []string{"docx"})
	t.Cleanup(worker.Stop)
	worker.executeTask(&TaskContext{Ctx: t.Context(), Task: running, TaskHandle: first})
	var status string
	var checkpoints int
	if err := f.Read.QueryRow("SELECT status FROM ingestion_task WHERE id=?", task.ID).Scan(&status); err != nil || status != common.RUNNING {
		t.Fatal("checkpoint failure committed task state", status, err)
	}
	if err := f.Read.QueryRow("SELECT COUNT(*) FROM ingestion_task_log WHERE task_id=?", task.ID).Scan(&checkpoints); err != nil || checkpoints != 0 {
		t.Fatal("failed transaction left checkpoint", checkpoints, err)
	}
	second := fetch()
	if second.GetMessage().TaskID != task.ID {
		t.Fatal("NACK did not redeliver actual task")
	}
	consumer, err := stream.Consumer(t.Context(), "RAGFLOW_CONSUMER")
	if err != nil {
		t.Fatal(err)
	}
	info, err := consumer.Info(t.Context())
	if err != nil || info.NumRedelivered < 1 {
		t.Fatal("independent redelivery readback", err)
	}
	if err := f.DB.Exec("DROP TRIGGER reject_ingestion_checkpoint").Error; err != nil {
		t.Fatal(err)
	}
	worker.executeTask(&TaskContext{Ctx: t.Context(), Task: running, TaskHandle: second})
	if err := f.Read.QueryRow("SELECT status FROM ingestion_task WHERE id=?", task.ID).Scan(&status); err != nil || status != common.FAILED {
		t.Fatal("unsupported execution falsely succeeded", status, err)
	}
	if err := f.Read.QueryRow("SELECT COUNT(*) FROM ingestion_task_log WHERE task_id=?", task.ID).Scan(&checkpoints); err != nil || checkpoints != 1 {
		t.Fatal("failure checkpoint not atomic", checkpoints, err)
	}
	deadline := time.Now().Add(3 * time.Second)
	for {
		info, err := stream.Info(t.Context())
		if err != nil {
			t.Fatal(err)
		}
		if info.State.Msgs == 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("ACK did not retire message")
		}
		time.Sleep(10 * time.Millisecond)
	}
	if err := queue.PublishTask("tasks.RAGFLOW", []byte(`{"task_id":17}`)); err != nil {
		t.Fatal(err)
	}
	if _, err := queue.GetMessages(1); err == nil {
		t.Fatal("malformed task returned success")
	}
	if err := stream.Purge(t.Context()); err != nil {
		t.Fatal(err)
	}
	endToEnd := &entity.IngestionTask{ID: "lifecycle-task", UserID: "owner", DocumentID: "lifecycle-doc", DatasetID: "dataset", Status: common.CREATED}
	if err := f.DB.Create(endToEnd).Error; err != nil {
		t.Fatal(err)
	}
	publish(endToEnd.ID)
	loop := NewIngestor("lifecycle", 1, []string{"docx"})
	done := make(chan error, 1)
	go func() { done <- loop.Start() }()
	t.Cleanup(loop.Stop)
	deadline = time.Now().Add(5 * time.Second)
	for {
		if err := f.Read.QueryRow("SELECT status FROM ingestion_task WHERE id=?", endToEnd.ID).Scan(&status); err != nil {
			t.Fatal(err)
		}
		if status == common.FAILED {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("actual worker did not persist explicit failure", status)
		}
		time.Sleep(10 * time.Millisecond)
	}
	loop.Stop()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("worker did not stop")
	}
	f.Journal.Event("actual-nats-mysql-acceptance", map[string]any{"transport": true, "rollback": true, "redelivery": true, "unsupported_failed": true, "worker_stopped": true})
}
