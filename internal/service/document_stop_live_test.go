package service

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	"multirag/internal/entity"

	"gorm.io/gorm"
)

// SQL transactions and row locks, Redis scripts/nonces and their readbacks are
// real. Index/object bytes are controlled and deliberately outside stop's scope.
type stopLiveExternalSnapshot struct {
	objects map[string]string
	index   string
	queue   string
}

func stopLiveExternalState(t *testing.T, f *deleteLiveFixture) stopLiveExternalSnapshot {
	t.Helper()
	f.index.mu.Lock()
	index, err := json.Marshal([]any{f.index.datasets, f.index.rows, f.index.metadata})
	f.index.mu.Unlock()
	if err != nil {
		t.Fatal(err)
	}
	queue, err := f.redis.XRange(context.Background(), f.queue, "-", "+").Result()
	if err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(queue)
	if err != nil {
		t.Fatal(err)
	}
	return stopLiveExternalSnapshot{objects: f.objects.snapshot(), index: string(index), queue: string(raw)}
}

func stopLiveAssertExternalState(t *testing.T, f *deleteLiveFixture, before stopLiveExternalSnapshot) {
	t.Helper()
	if !reflect.DeepEqual(before, stopLiveExternalState(t, f)) || f.objects.calls != 0 {
		t.Fatal("stop changed index, object bytes or queue contents")
	}
}

func stopLiveAssertNoFlags(t *testing.T, f *deleteLiveFixture) {
	t.Helper()
	count, err := f.redis.Exists(context.Background(), f.taskA+"-cancel", f.taskB+"-cancel").Result()
	if err != nil || count != 0 {
		t.Fatal("unexpected cancellation fence", count, err)
	}
}

func TestDocumentStopLiveAtomicDeduplicatedBatch(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	external := stopLiveExternalState(t, f)
	svc := NewDocumentStopService(f.db, f.redis)
	count, err := svc.StopParseDocuments(context.Background(), f.kb, f.owner, []string{f.b, f.a, f.a})
	if err != nil || count != 2 {
		t.Fatal("complete distinct acknowledgement required", count, err)
	}
	f.assertCanceledParents()
	var canceled int
	if err := f.read.QueryRow("SELECT count(*) FROM t_ai_documents WHERE id IN ($1,$2) AND run='2' AND progress=0", f.a, f.b).Scan(&canceled); err != nil || canceled != 2 {
		t.Fatal("document cancellation did not commit", canceled, err)
	}
	for _, task := range []string{f.taskA, f.taskB} {
		value, err := f.redis.Get(context.Background(), task+"-cancel").Result()
		if err != nil || value == "" || value == "x" {
			t.Fatal("missing attempt-specific cancellation nonce", value, err)
		}
		ttl, err := f.redis.TTL(context.Background(), task+"-cancel").Result()
		if err != nil || ttl <= 0 || ttl > taskRuntimeTTL {
			t.Fatal("cancellation lifetime must be bounded", ttl, err)
		}
	}
	stopLiveAssertExternalState(t, f, external)
	committed := f.state()
	nonces, err := f.redis.MGet(context.Background(), f.taskA+"-cancel", f.taskB+"-cancel").Result()
	if err != nil {
		t.Fatal(err)
	}
	if count, err := svc.StopParseDocuments(context.Background(), f.kb, f.owner, []string{f.a, f.b}); err != nil || count != 2 || f.state() != committed {
		t.Fatal("repeated stop must preserve committed cancellation", count, err)
	}
	after, err := f.redis.MGet(context.Background(), f.taskA+"-cancel", f.taskB+"-cancel").Result()
	if err != nil || !reflect.DeepEqual(nonces, after) {
		t.Fatal("repeated stop replaced a cancellation nonce", err)
	}
	f.journal.Event("business-readback", map[string]any{"distinct_acknowledged": count, "canceled_documents": canceled, "parents_and_counters_preserved": true, "queue_preserved": true, "controlled_index_and_objects_preserved": true, "repeat_preserved_nonce": true, "worker_termination_proven": false})
}

func TestDocumentStopLiveRejectsEntireBatchBeforeEffects(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	svc := NewDocumentStopService(f.db, f.redis)
	before, external := f.state(), stopLiveExternalState(t, f)
	for _, test := range []struct {
		name string
		user string
		ids  []string
		want error
	}{
		{"missing", f.owner, []string{f.a, f.prefix + "missing"}, ErrDocumentDeleteForbidden},
		{"foreign", f.owner, []string{f.a, f.foreign}, ErrDocumentDeleteForbidden},
		{"unauthorized", f.prefix + "stranger", []string{f.a, f.b}, ErrDocumentDeleteForbidden},
		{"nil", f.owner, nil, ErrInvalidDocumentStopRequest},
		{"empty", f.owner, []string{}, ErrInvalidDocumentStopRequest},
		{"invalid_id", f.owner, []string{f.a, " "}, ErrInvalidDocumentStopRequest},
	} {
		t.Run(test.name, func(t *testing.T) {
			if count, err := svc.StopParseDocuments(context.Background(), f.kb, test.user, test.ids); count != 0 || !errors.Is(err, test.want) {
				t.Fatal("invalid batch was accepted", count, err)
			}
			if f.state() != before {
				t.Fatal("rejected batch changed SQL")
			}
			stopLiveAssertNoFlags(t, f)
			stopLiveAssertExternalState(t, f, external)
		})
	}
	f.journal.Event("denial-readback", map[string]any{"mixed_batches_atomic": true, "explicit_empty_rejected": true, "sql_and_external_side_effects": 0})
}

func TestDocumentStopLiveSQLFailureCompensatesOnlyOwnNonce(t *testing.T) {
	for _, existing := range []string{"cancel_flag", "runtime_binding"} {
		t.Run(existing, func(t *testing.T) {
			f := documentDeleteLiveFixture(t)
			ctx := context.Background()
			runtime := ""
			if existing == "cancel_flag" {
				if err := f.redis.Set(ctx, f.taskA+"-cancel", "previous-cancellation", taskRuntimeTTL).Err(); err != nil {
					t.Fatal(err)
				}
			} else {
				raw, err := json.Marshal(taskBinding{Version: 1, PrincipalID: f.owner, TenantID: f.owner, ResourceID: f.a, Kind: "agent", State: "active"})
				if err != nil {
					t.Fatal(err)
				}
				runtime = string(raw)
				if err := f.redis.Set(ctx, taskRuntimePrefix+f.taskA, runtime, taskRuntimeTTL).Err(); err != nil {
					t.Fatal(err)
				}
			}
			before, external := f.state(), stopLiveExternalState(t, f)
			constraint := f.prefix + "stopguard"
			f.journal.Event("reserve-constraint", map[string]any{"table": "t_ai_tasks", "constraint": constraint})
			if err := f.db.Exec(fmt.Sprintf("ALTER TABLE t_ai_tasks ADD CONSTRAINT %s CHECK (id <> '%s' OR progress >= 0)", constraint, f.taskB)).Error; err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() {
				if err := f.db.Exec("ALTER TABLE t_ai_tasks DROP CONSTRAINT " + constraint).Error; err != nil {
					t.Error(err)
				}
				var remaining int
				if err := f.read.QueryRow("SELECT count(*) FROM pg_constraint WHERE conname=$1", constraint).Scan(&remaining); err != nil || remaining != 0 {
					t.Error("constraint cleanup readback", remaining, err)
				}
				f.journal.Event("constraint-cleanup-readback", map[string]any{"constraint": constraint, "remaining": remaining})
			})
			if count, err := NewDocumentStopService(f.db, f.redis).StopParseDocuments(ctx, f.kb, f.owner, []string{f.a, f.b}); count != 0 || err == nil {
				t.Fatal("failed SQL batch reported success", count, err)
			}
			if f.state() != before {
				t.Fatal("failed batch committed partial SQL cancellation")
			}
			if count, err := f.redis.Exists(ctx, f.taskB+"-cancel").Result(); err != nil || count != 0 {
				t.Fatal("failed batch leaked its own nonce", count, err)
			}
			if existing == "cancel_flag" {
				value, err := f.redis.Get(ctx, f.taskA+"-cancel").Result()
				if err != nil || value != "previous-cancellation" {
					t.Fatal("rollback erased another cancellation", value, err)
				}
			} else {
				value, err := f.redis.Get(ctx, taskRuntimePrefix+f.taskA).Result()
				if err != nil || value != runtime {
					t.Fatal("runtime CAS compensation did not restore the original binding", err)
				}
				stopLiveAssertNoFlags(t, f)
			}
			stopLiveAssertExternalState(t, f, external)
			f.journal.Event("failure-readback", map[string]any{"injected_boundary": "SQL task update", "sql_rolled_back": true, "own_nonce_absent": true, "previous_cancel_or_binding_preserved": true})
		})
	}
}

func TestDocumentStopLiveTerminalDocumentsRejectWithoutEffects(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	for _, row := range []struct {
		id  string
		run string
	}{{f.a, "3"}, {f.b, "4"}} {
		if err := f.db.Model(&entity.Document{}).Where("id = ?", row.id).Updates(map[string]any{"run": row.run, "progress": 1}).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := f.db.Model(&entity.Task{}).Where("id = ?", f.taskA).Update("progress", 1).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.db.Model(&entity.Task{}).Where("id = ?", f.taskB).Update("progress", -1).Error; err != nil {
		t.Fatal(err)
	}
	before, external := f.state(), stopLiveExternalState(t, f)
	// Terminal documents without unfinished tasks reject without needing Redis.
	svc := NewDocumentStopService(f.db, nil)
	for range 2 {
		if count, err := svc.StopParseDocuments(context.Background(), f.kb, f.owner, []string{f.b, f.a}); err == nil || count != 0 || f.state() != before {
			t.Fatal("terminal request was accepted or changed persisted state", count, err)
		}
	}
	stopLiveAssertNoFlags(t, f)
	stopLiveAssertExternalState(t, f, external)
}

func TestDocumentStopLiveCanceledBeforeStorage(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	before, external := f.state(), stopLiveExternalState(t, f)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if count, err := NewDocumentStopService(f.db, f.redis).StopParseDocuments(ctx, f.kb, f.owner, []string{f.a, f.b}); count != 0 || !errors.Is(err, context.Canceled) || f.state() != before {
		t.Fatal("pre-canceled stop changed SQL", count, err)
	}
	stopLiveAssertNoFlags(t, f)
	stopLiveAssertExternalState(t, f, external)
}

func TestDocumentStopLiveLocksDocumentBeforeTask(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	f.journal.Event("reserve-sql-locks", map[string]any{"document_ids": []string{f.a}, "task_ids": []string{f.taskA}, "scope": "transactions rolled back or committed before case cleanup"})
	blocker, err := f.read.BeginTx(ctx, &sql.TxOptions{})
	if err != nil {
		t.Fatal(err)
	}
	defer blocker.Rollback()
	var id string
	if err := blocker.QueryRowContext(ctx, "SELECT id FROM t_ai_documents WHERE id=$1 FOR UPDATE", f.a).Scan(&id); err != nil {
		t.Fatal(err)
	}
	var blockerPID int
	if err := blocker.QueryRowContext(ctx, "SELECT pg_backend_pid()").Scan(&blockerPID); err != nil {
		t.Fatal(err)
	}
	type result struct {
		count int
		err   error
	}
	done := make(chan result, 1)
	go func() {
		count, err := NewDocumentStopService(f.db, f.redis).StopParseDocuments(ctx, f.kb, f.owner, []string{f.a})
		done <- result{count: count, err: err}
	}()
	defer func() {
		cancel()
		blocker.Rollback()
		select {
		case <-done:
		case <-time.After(2 * time.Second):
			t.Error("stop goroutine did not drain")
		}
	}()
	// Observe the real blocked query; elapsed time alone does not prove lock order.
	waitCtx, waitCancel := context.WithTimeout(ctx, 3*time.Second)
	defer waitCancel()
	for {
		var blocked bool
		if err := f.read.QueryRowContext(waitCtx, "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND $1 = ANY(pg_blocking_pids(pid)))", blockerPID).Scan(&blocked); err != nil {
			t.Fatal("stop did not reach the document row lock", err)
		}
		if blocked {
			break
		}
		select {
		case result := <-done:
			done <- result
			t.Fatal("stop completed while its document was locked", result.count, result.err)
		case <-waitCtx.Done():
			t.Fatal("stop did not block on document", waitCtx.Err())
		case <-time.After(5 * time.Millisecond):
		}
	}
	taskReader, err := f.read.BeginTx(ctx, &sql.TxOptions{})
	if err != nil {
		t.Fatal(err)
	}
	defer taskReader.Rollback()
	if err := taskReader.QueryRowContext(ctx, "SELECT id FROM t_ai_tasks WHERE id=$1 FOR UPDATE NOWAIT", f.taskA).Scan(&id); err != nil {
		t.Fatal("stop locked task before obtaining its document", err)
	}
	if err := taskReader.Rollback(); err != nil {
		t.Fatal(err)
	}
	if err := blocker.Rollback(); err != nil {
		t.Fatal(err)
	}
	finalResult := <-done
	done <- finalResult
	if finalResult.err != nil || finalResult.count != 1 {
		t.Fatal("stop failed after lock release", finalResult.count, finalResult.err)
	}
	var run string
	var progress float64
	if err := f.read.QueryRowContext(ctx, "SELECT d.run,t.progress FROM t_ai_documents d JOIN t_ai_tasks t ON t.doc_id=d.id WHERE d.id=$1 AND t.id=$2", f.a, f.taskA).Scan(&run, &progress); err != nil || run != "2" || progress != -1 {
		t.Fatal("stop did not commit after lock release", run, progress, err)
	}
	var siblingRun string
	if err := f.read.QueryRowContext(ctx, "SELECT run FROM t_ai_documents WHERE id=$1", f.b).Scan(&siblingRun); err != nil || siblingRun != "1" {
		t.Fatal("stop touched an unselected document", siblingRun, err)
	}
	f.journal.Event("lock-readback", map[string]any{"document_block_observed": true, "task_lock_available_while_document_blocked": true, "selected_run": run, "selected_task_progress": progress, "unselected_run": siblingRun, "request_goroutine_drained": true})
}

func TestDocumentStopLiveRuntimeOwnerMismatchHasNoEffects(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	ctx := context.Background()
	raw, err := json.Marshal(taskBinding{Version: 1, PrincipalID: f.owner, TenantID: f.owner, ResourceID: f.foreign, Kind: "agent", State: "active"})
	if err != nil {
		t.Fatal(err)
	}
	if err := f.redis.Set(ctx, taskRuntimePrefix+f.taskB, raw, taskRuntimeTTL).Err(); err != nil {
		t.Fatal(err)
	}
	before := f.state()
	if count, err := NewDocumentStopService(f.db, f.redis).StopParseDocuments(ctx, f.kb, f.owner, []string{f.a, f.b}); count != 0 || !errors.Is(err, ErrTaskForbidden) || f.state() != before {
		t.Fatal("runtime owner mismatch must reject before any SQL effect", count, err)
	}
	stopLiveAssertNoFlags(t, f)
	value, err := f.redis.Get(ctx, taskRuntimePrefix+f.taskB).Result()
	if err != nil || strings.TrimSpace(value) != string(raw) {
		t.Fatal("rejected request rewrote foreign runtime binding", err)
	}
}

func TestDocumentStopLiveUnfinishedTasksCancelRegardlessOfRun(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	for _, row := range []struct{ id, run string }{{f.a, "0"}, {f.b, "3"}} {
		if err := f.db.Model(&entity.Document{}).Where("id = ?", row.id).Update("run", row.run).Error; err != nil {
			t.Fatal(err)
		}
	}
	external := stopLiveExternalState(t, f)
	if count, err := NewDocumentStopService(f.db, f.redis).StopParseDocuments(context.Background(), f.kb, f.owner, []string{f.a, f.b}); err != nil || count != 2 {
		t.Fatal("unfinished tasks must be cancelable despite stale document run", count, err)
	}
	f.assertCanceledParents()
	var canceled int
	if err := f.read.QueryRow("SELECT count(*) FROM t_ai_documents WHERE id IN ($1,$2) AND run='2'", f.a, f.b).Scan(&canceled); err != nil || canceled != 2 {
		t.Fatal("unfinished task parent not canceled", canceled, err)
	}
	stopLiveAssertExternalState(t, f, external)
}

func TestDocumentStopLiveMixedTerminalBatchRejectsBeforeCancellation(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	if err := f.db.Model(&entity.Document{}).Where("id = ?", f.a).Updates(map[string]any{"run": "3", "progress": 1}).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.db.Model(&entity.Task{}).Where("id = ?", f.taskA).Update("progress", 1).Error; err != nil {
		t.Fatal(err)
	}
	before, external := f.state(), stopLiveExternalState(t, f)
	if count, err := NewDocumentStopService(f.db, f.redis).StopParseDocuments(context.Background(), f.kb, f.owner, []string{f.b, f.a}); err == nil || count != 0 || f.state() != before {
		t.Fatal("mixed terminal batch partially canceled active sibling", count, err)
	}
	stopLiveAssertNoFlags(t, f)
	stopLiveAssertExternalState(t, f, external)
}

func TestDocumentStopLiveDisabledActiveDocumentRemainsCancelable(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	if err := f.db.Model(&entity.Document{}).Where("id = ?", f.b).Update("status", "0").Error; err != nil {
		t.Fatal(err)
	}
	external := stopLiveExternalState(t, f)
	if count, err := NewDocumentStopService(f.db, f.redis).StopParseDocuments(context.Background(), f.kb, f.owner, []string{f.a, f.b}); err != nil || count != 2 {
		t.Fatal("disabled retrieval status must not prevent stopping its active task", count, err)
	}
	f.assertCanceledParents()
	var status, run string
	if err := f.read.QueryRow("SELECT status,run FROM t_ai_documents WHERE id=$1", f.b).Scan(&status, &run); err != nil || status != "0" || run != "2" {
		t.Fatal("stop must preserve disabled status while canceling its producer", status, run, err)
	}
	stopLiveAssertExternalState(t, f, external)
	f.journal.Event("disabled-stop-readback", map[string]any{"disabled_status_preserved": status, "producer_run": run, "task_cancellation_accepted": true})
}

func TestDocumentStopLiveFinishedRuntimeRejectsActiveSQLBatch(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	ctx := context.Background()
	raw, err := json.Marshal(taskBinding{Version: 1, PrincipalID: f.owner, TenantID: f.owner, ResourceID: f.b, Kind: "agent", State: "finished"})
	if err != nil {
		t.Fatal(err)
	}
	if err := f.redis.Set(ctx, taskRuntimePrefix+f.taskB, raw, taskRuntimeTTL).Err(); err != nil {
		t.Fatal(err)
	}
	before, external := f.state(), stopLiveExternalState(t, f)
	if count, err := NewDocumentStopService(f.db, f.redis).StopParseDocuments(ctx, f.kb, f.owner, []string{f.a, f.b}); count != 0 || err == nil || f.state() != before {
		t.Fatal("finished runtime with active SQL task must reject the entire batch", count, err)
	}
	stopLiveAssertNoFlags(t, f)
	value, err := f.redis.Get(ctx, taskRuntimePrefix+f.taskB).Result()
	if err != nil || value != string(raw) {
		t.Fatal("rejected stop changed finished runtime", err)
	}
	stopLiveAssertExternalState(t, f, external)
	f.journal.Event("finished-runtime-readback", map[string]any{"active_sql_with_finished_runtime_rejected": true, "sql_and_redis_unchanged": true})
}

var errStopLiveCommitAckLost = errors.New("injected stop commit acknowledgement loss")

type stopLiveCommitAckLossPool struct {
	gorm.ConnPool
	pool      *sql.DB
	committed *bool
}

func (p *stopLiveCommitAckLossPool) BeginTx(ctx context.Context, opts *sql.TxOptions) (gorm.ConnPool, error) {
	tx, err := p.pool.BeginTx(ctx, opts)
	if err != nil {
		return nil, err
	}
	return &stopLiveCommitAckLossTx{ConnPool: tx, TxCommitter: tx, committed: p.committed}, nil
}

type stopLiveCommitAckLossTx struct {
	gorm.ConnPool
	gorm.TxCommitter
	committed *bool
}

func (tx *stopLiveCommitAckLossTx) Commit() error {
	if err := tx.TxCommitter.Commit(); err != nil {
		return err
	}
	*tx.committed = true
	return errStopLiveCommitAckLost
}

func TestDocumentStopLiveCommitAckLossPreservesCancellationFences(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	ctx := context.Background()
	external := stopLiveExternalState(t, f)
	pool, err := f.db.DB()
	if err != nil {
		t.Fatal(err)
	}
	committed := false
	wrappedPool := &stopLiveCommitAckLossPool{ConnPool: pool, pool: pool, committed: &committed}
	// Context clones Statement as well as Config, so fixture cleanup continues
	// through its original unwrapped connection pool.
	wrapped := f.db.Session(&gorm.Session{NewDB: true, Context: ctx})
	wrapped.Config.ConnPool = wrappedPool
	wrapped.Statement.ConnPool = wrappedPool
	f.journal.Event("inject-commit-ack-loss", map[string]any{"document_ids": []string{f.a, f.b}, "task_ids": []string{f.taskA, f.taskB}, "boundary": "return error after actual SQL Commit"})
	count, err := NewDocumentStopService(wrapped, f.redis).StopParseDocuments(ctx, f.kb, f.owner, []string{f.a, f.b})
	if count != 0 || !errors.Is(err, ErrDocumentStopUncertain) || !errors.Is(err, errStopLiveCommitAckLost) || !committed {
		t.Fatal("lost commit acknowledgement must report uncertainty", count, err, committed)
	}
	// These reads use the separate SQL pool and Redis commands. The injected
	// error is evidence about the acknowledgement, not about rollback.
	f.assertCanceledParents()
	var canceled int
	if err := f.read.QueryRow("SELECT count(*) FROM t_ai_documents WHERE id IN ($1,$2) AND run='2'", f.a, f.b).Scan(&canceled); err != nil || canceled != 2 {
		t.Fatal("the injected acknowledgement loss did not follow a real commit", canceled, err)
	}
	nonces, err := f.redis.MGet(ctx, f.taskA+"-cancel", f.taskB+"-cancel").Result()
	if err != nil || len(nonces) != 2 || nonces[0] == nil || nonces[1] == nil {
		t.Fatal("uncertain commit incorrectly compensated committed fences", err)
	}
	if count, err := NewDocumentStopService(f.db, f.redis).StopParseDocuments(ctx, f.kb, f.owner, []string{f.a, f.b}); count != 2 || err != nil {
		t.Fatal("retry after lost acknowledgement did not recover", count, err)
	}
	after, err := f.redis.MGet(ctx, f.taskA+"-cancel", f.taskB+"-cancel").Result()
	if err != nil || !reflect.DeepEqual(nonces, after) {
		t.Fatal("retry after commit uncertainty replaced existing fences", err)
	}
	stopLiveAssertExternalState(t, f, external)
	f.journal.Event("commit-ack-loss-readback", map[string]any{"actual_sql_commit_observed": true, "canceled_documents": canceled, "both_fences_preserved": true, "retry_acknowledged": true, "worker_termination_proven": false})
}

func TestDocumentStopLiveMembershipRevocationSerializesBeforeEffects(t *testing.T) {
	f := documentDeleteLiveFixture(t)
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	memberID, principal, active := f.prefix+"member", f.prefix+"admin", "1"
	var existing int
	if err := f.read.QueryRow("SELECT count(*) FROM t_ai_user_tenants WHERE id=$1", memberID).Scan(&existing); err != nil || existing != 0 {
		t.Fatal("membership identity not absent", existing, err)
	}
	f.journal.Event("reserve-membership", map[string]any{"table": "t_ai_user_tenants", "id": memberID, "principal": principal, "tenant_id": f.owner})
	t.Cleanup(func() {
		if err := f.db.Where("id = ? AND tenant_id = ? AND user_id = ?", memberID, f.owner, principal).Delete(&entity.UserTenant{}).Error; err != nil {
			t.Error(err)
		}
		var remaining int
		if err := f.read.QueryRow("SELECT count(*) FROM t_ai_user_tenants WHERE id=$1", memberID).Scan(&remaining); err != nil || remaining != 0 {
			t.Error("membership cleanup readback", remaining, err)
		}
		f.journal.Event("membership-cleanup-readback", map[string]any{"id": memberID, "remaining": remaining})
	})
	if err := f.db.Create(&entity.UserTenant{ID: memberID, UserID: principal, TenantID: f.owner, Role: "admin", InvitedBy: f.owner, Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	// A future generation proves GREATEST increments even when wall time is behind.
	seedTime := time.Now().Add(24 * time.Hour).UnixMilli()
	if _, err := f.read.ExecContext(ctx, "UPDATE t_ai_documents SET update_time=$1 WHERE id=$2", seedTime, f.a); err != nil {
		t.Fatal(err)
	}
	before, external := f.state(), stopLiveExternalState(t, f)
	f.journal.Event("reserve-sql-locks", map[string]any{"membership_id": memberID, "principal": principal, "document_ids": []string{f.a}, "task_ids": []string{f.taskA}})
	revoker, err := f.read.BeginTx(ctx, &sql.TxOptions{})
	if err != nil {
		t.Fatal(err)
	}
	defer revoker.Rollback()
	if _, err := revoker.ExecContext(ctx, "UPDATE t_ai_user_tenants SET status='0' WHERE id=$1", memberID); err != nil {
		t.Fatal(err)
	}
	var blockerPID int
	if err := revoker.QueryRowContext(ctx, "SELECT pg_backend_pid()").Scan(&blockerPID); err != nil {
		t.Fatal(err)
	}
	type stopResult struct {
		n   int
		err error
	}
	done, drained := make(chan stopResult, 1), false
	go func() {
		n, err := NewDocumentStopService(f.db, f.redis).StopParseDocuments(ctx, f.kb, principal, []string{f.a})
		done <- stopResult{n, err}
	}()
	defer func() {
		cancel()
		revoker.Rollback()
		if !drained {
			select {
			case <-done:
			case <-time.After(2 * time.Second):
				t.Error("authorization request did not drain")
			}
		}
	}()
	waitCtx, waitCancel := context.WithTimeout(ctx, 3*time.Second)
	defer waitCancel()
	for {
		var blocked bool
		if err := f.read.QueryRowContext(waitCtx, "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND $1=ANY(pg_blocking_pids(pid)))", blockerPID).Scan(&blocked); err != nil {
			t.Fatal("membership wait not observed", err)
		}
		if blocked {
			break
		}
		select {
		case result := <-done:
			drained = true
			t.Fatal("stop did not wait for membership lock", result.n, result.err)
		case <-waitCtx.Done():
			t.Fatal(waitCtx.Err())
		case <-time.After(5 * time.Millisecond):
		}
	}
	stopLiveAssertNoFlags(t, f)
	if err := revoker.Commit(); err != nil {
		t.Fatal(err)
	}
	result := <-done
	drained = true
	if result.n != 0 || !errors.Is(result.err, ErrDocumentDeleteForbidden) || f.state() != before {
		t.Fatal("revoked admin canceled documents", result.n, result.err)
	}
	stopLiveAssertNoFlags(t, f)
	stopLiveAssertExternalState(t, f, external)
	// The authorized owner can still cancel, with a strictly newer generation.
	if n, err := NewDocumentStopService(f.db, f.redis).StopParseDocuments(ctx, f.kb, f.owner, []string{f.a}); n != 1 || err != nil {
		t.Fatal("owner cancellation failed", n, err)
	}
	var bumped int64
	var run, status string
	if err := f.read.QueryRowContext(ctx, "SELECT d.update_time,d.run,(SELECT status FROM t_ai_user_tenants WHERE id=$1) FROM t_ai_documents d WHERE d.id=$2", memberID, f.a).Scan(&bumped, &run, &status); err != nil || bumped <= seedTime || run != "2" || status != "0" {
		t.Fatal("generation or authorization readback failed", bumped, seedTime, run, status, err)
	}
	stopLiveAssertExternalState(t, f, external)
	f.journal.Event("authorization-race-readback", map[string]any{"membership_wait_observed": true, "revoked_admin_side_effects": 0, "owner_stop_accepted": true, "seed_update_time": seedTime, "committed_update_time": bumped, "strict_generation_increase": bumped > seedTime, "request_goroutine_drained": drained})
}
