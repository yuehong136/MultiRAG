package service

import (
	"context"
	"database/sql"
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"gorm.io/gorm"
	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/testsupport/scratch"
)

var errRecoveryCommitFault = errors.New("controlled commit acknowledgement loss")

// Only the first transaction is faulted. SQL, row locks, COMMIT and ROLLBACK
// still use the real scratch PostgreSQL connection; subsequent readback uses
// another real transaction from the pool.
type recoveryCommitFaultPool struct {
	*sql.DB
	once     sync.Once
	commit   func(*sql.Tx) error
	keepOpen bool
}

func (p *recoveryCommitFaultPool) BeginTx(ctx context.Context, options *sql.TxOptions) (gorm.ConnPool, error) {
	tx, err := p.DB.BeginTx(ctx, options)
	if err != nil {
		return nil, err
	}
	fault := false
	p.once.Do(func() { fault = true })
	if fault {
		return &recoveryCommitFaultTx{Tx: tx, commit: p.commit, keepOpen: p.keepOpen}, nil
	}
	return tx, nil
}

type recoveryCommitFaultTx struct {
	*sql.Tx
	commit   func(*sql.Tx) error
	keepOpen bool
}

func (t *recoveryCommitFaultTx) Commit() error { return t.commit(t.Tx) }
func (t *recoveryCommitFaultTx) Rollback() error {
	if t.keepOpen {
		return errRecoveryCommitFault
	}
	return t.Tx.Rollback()
}

type recoveryCommitFixture struct {
	*scratch.Services
	service                         *PasswordRecoveryService
	email, session, oldHash, userID string
	request                         ForgotResetPasswordRequest
	hashes                          atomic.Int32
}

func newRecoveryCommitFixture(t *testing.T) *recoveryCommitFixture {
	t.Helper()
	f := &recoveryCommitFixture{Services: scratch.New(t), session: strings.Repeat("a", 64)}
	if err := f.DB.AutoMigrate(&entity.User{}); err != nil {
		t.Fatal(err)
	}
	f.userID = f.Prefix + "user"
	f.email = f.Prefix + "@example.test"
	status := "1"
	hash, err := NewUserService().HashPassword("before commit fault")
	if err != nil {
		t.Fatal(err)
	}
	f.oldHash = hash
	if err := f.DB.Create(&entity.User{ID: f.userID, Email: f.email, Nickname: "commit-fault", Password: &hash, AccountKind: "local", Status: &status}).Error; err != nil {
		t.Fatal(err)
	}
	f.service = NewPasswordRecoveryService(f.DB, f.Redis, common.SMTPConfig{})
	f.service.hashPassword = func(password string) (string, error) { f.hashes.Add(1); return NewUserService().HashPassword(password) }
	f.request = ForgotResetPasswordRequest{Email: f.email, NewPassword: "after commit fault", ConfirmNewPassword: "after commit fault"}
	f.Journal.Event("reserve-recovery-commit-fault", map[string]any{"database": f.Database, "user": f.userID, "bound_session": true, "ttl_seconds": 90})
	if err := f.Redis.Set(t.Context(), recoveryKeys(f.email)[5], recoveryProof(f.session), 90*time.Second).Err(); err != nil {
		t.Fatal(err)
	}
	return f
}

func (f *recoveryCommitFixture) fault(t *testing.T, keepOpen bool, commit func(*sql.Tx) error) {
	t.Helper()
	pool, err := f.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	controlled := &recoveryCommitFaultPool{DB: pool, commit: commit, keepOpen: keepOpen}
	db := f.DB.Session(&gorm.Session{NewDB: true, Context: t.Context()})
	db.Config.ConnPool = controlled
	db.Statement.ConnPool = controlled
	f.service.db = db
}

func (f *recoveryCommitFixture) password(t *testing.T) string {
	t.Helper()
	var value string
	if err := f.Read.QueryRowContext(t.Context(), "SELECT password FROM t_ai_users WHERE id=$1", f.userID).Scan(&value); err != nil {
		t.Fatal(err)
	}
	return value
}

func TestForgotResetUnknownCommitDoesNotRestoreProof(t *testing.T) {
	f := newRecoveryCommitFixture(t)
	f.fault(t, false, func(tx *sql.Tx) error {
		if err := tx.Rollback(); err != nil {
			return err
		}
		return errRecoveryCommitFault
	})
	if code, err := f.service.ResetPassword(t.Context(), f.request, f.session); err == nil || code != common.CodeServerError {
		t.Fatal("unknown commit acknowledged success", code)
	}
	keys := recoveryKeys(f.email)
	if exists, err := f.Redis.Exists(t.Context(), keys[5]).Result(); err != nil || exists != 0 {
		t.Fatal("unknown commit restored a consumed proof")
	}
	if exists, err := f.Redis.Exists(t.Context(), keys[7]).Result(); err != nil || exists != 1 {
		t.Fatal("unknown commit lost its claim fence")
	}
	if f.password(t) != f.oldHash {
		t.Fatal("controlled rolled-back write reached SQL")
	}
	if code, err := f.service.ResetPassword(t.Context(), f.request, f.session); err == nil || code != common.CodeAuthenticationError {
		t.Fatal("unknown commit reused consumed client proof")
	}
	if f.hashes.Load() != 1 || f.password(t) != f.oldHash {
		t.Fatal("unknown commit performed a second password update")
	}
	f.Journal.Event("recovery-unknown-commit-verified", map[string]any{"proof_absent": true, "claim_retained": true, "second_hash_calls": 0, "actual_sql_unchanged": true})
}

func TestForgotResetPendingCommitWaitsForLockedReadback(t *testing.T) {
	f := newRecoveryCommitFixture(t)
	entered := make(chan *sql.Tx, 1)
	f.fault(t, true, func(tx *sql.Tx) error { entered <- tx; return errRecoveryCommitFault })
	type result struct {
		code common.ErrorCode
		err  error
	}
	done := make(chan result, 1)
	var pending *sql.Tx
	finished := false
	t.Cleanup(func() {
		if pending != nil {
			pending.Rollback()
		}
		if !finished {
			select {
			case <-done:
			case <-time.After(3 * time.Second):
				t.Error("pending recovery survived cleanup")
			}
		}
	})
	go func() {
		code, err := f.service.ResetPassword(t.Context(), f.request, f.session)
		done <- result{code, err}
	}()
	select {
	case pending = <-entered:
	case <-time.After(3 * time.Second):
		t.Fatal("controlled commit boundary not entered")
	}
	// A plain MVCC SELECT sees the old password while this backend transaction
	// still owns the row. That is not evidence that COMMIT cannot finish.
	if f.password(t) != f.oldHash {
		t.Fatal("controlled MVCC precondition")
	}
	deadline := time.Now().Add(2 * time.Second)
	blocked := false
	for !blocked && time.Now().Before(deadline) {
		if err := f.Read.QueryRowContext(t.Context(), "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=$1 AND wait_event_type='Lock' AND query ILIKE '%t_ai_users%' AND query ILIKE '%FOR UPDATE%')", f.Database).Scan(&blocked); err != nil {
			t.Fatal(err)
		}
		select {
		case got := <-done:
			finished = true
			t.Fatal("reset returned before pending COMMIT resolved", got.code)
		default:
		}
		if !blocked {
			time.Sleep(10 * time.Millisecond)
		}
	}
	if !blocked {
		t.Fatal("independent FOR UPDATE did not wait for original transaction")
	}
	keys := recoveryKeys(f.email)
	if proof, _ := f.Redis.Exists(t.Context(), keys[5]).Result(); proof != 0 {
		t.Fatal("proof restored before original COMMIT resolved")
	}
	if err := pending.Commit(); err != nil {
		t.Fatal("resolve actual pending SQL commit", err)
	}
	select {
	case got := <-done:
		finished = true
		if got.err != nil || got.code != common.CodeSuccess {
			t.Fatal("committed acknowledgement loss not recognized", got.code, got.err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("locked readback did not resolve")
	}
	if !NewUserService().VerifyPassword(f.password(t), f.request.NewPassword) {
		t.Fatal("committed hash independent readback")
	}
	if exists, err := f.Redis.Exists(t.Context(), keys[5], keys[7]).Result(); err != nil || exists != 0 {
		t.Fatal("recognized commit retained proof or claim")
	}
	if code, err := f.service.ResetPassword(t.Context(), f.request, f.session); err == nil || code != common.CodeAuthenticationError {
		t.Fatal("committed acknowledgement loss reset replay")
	}
	if f.hashes.Load() != 1 {
		t.Fatal("committed acknowledgement loss caused a second password update")
	}
	f.Journal.Event("recovery-pending-commit-verified", map[string]any{"plain_mvcc_read_saw_old_hash": true, "independent_for_update_waited": true, "committed_ack_loss_recognized": true, "second_hash_calls": 0})
}

func TestForgotResetCommitReadbackDeadlineDoesNotRestoreProof(t *testing.T) {
	f := newRecoveryCommitFixture(t)
	entered := make(chan *sql.Tx, 1)
	f.fault(t, true, func(tx *sql.Tx) error { entered <- tx; return errRecoveryCommitFault })
	type result struct {
		code common.ErrorCode
		err  error
	}
	done := make(chan result, 1)
	var pending *sql.Tx
	finished := false
	t.Cleanup(func() {
		if pending != nil {
			pending.Rollback()
		}
		if !finished {
			select {
			case <-done:
			case <-time.After(3 * time.Second):
				t.Error("unresolved recovery survived cleanup")
			}
		}
	})
	go func() {
		code, err := f.service.ResetPassword(t.Context(), f.request, f.session)
		done <- result{code, err}
	}()
	select {
	case pending = <-entered:
	case <-time.After(3 * time.Second):
		t.Fatal("controlled commit boundary not entered")
	}
	started := time.Now()
	select {
	case got := <-done:
		finished = true
		if got.err == nil || got.code != common.CodeServerError || time.Since(started) < 4*time.Second {
			t.Fatal("unresolved commit bypassed bounded lock wait", got.code)
		}
	case <-time.After(8 * time.Second):
		t.Fatal("independent commit readback did not respect its deadline")
	}
	keys := recoveryKeys(f.email)
	if exists, err := f.Redis.Exists(t.Context(), keys[5]).Result(); err != nil || exists != 0 {
		t.Fatal("readback timeout restored a consumed proof")
	}
	if exists, err := f.Redis.Exists(t.Context(), keys[7]).Result(); err != nil || exists != 1 {
		t.Fatal("readback timeout lost the reset claim")
	}
	if err := pending.Rollback(); err != nil && !errors.Is(err, sql.ErrTxDone) {
		t.Fatal("resolve controlled pending transaction", err)
	}
	if code, err := f.service.ResetPassword(t.Context(), f.request, f.session); err == nil || code != common.CodeAuthenticationError {
		t.Fatal("readback timeout reused consumed proof")
	}
	if f.hashes.Load() != 1 || f.password(t) != f.oldHash {
		t.Fatal("readback timeout changed SQL or retried password hashing")
	}
	f.Journal.Event("recovery-commit-readback-deadline-verified", map[string]any{"bounded_seconds": 5, "proof_absent": true, "claim_retained": true, "second_hash_calls": 0})
}
