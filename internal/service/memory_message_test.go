package service

import (
	"context"
	"errors"
	"github.com/jackc/pgx/v5/pgconn"
	"sync"
	"testing"
	"time"

	"multirag/internal/dao"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/testsupport/scratch"
)

type controlledMemoryStore struct {
	calls          int
	tenant, memory string
	id             int64
	forgot         time.Time
	err            error
}

type drainingMemoryStore struct{ entered, release chan struct{} }

func (s *drainingMemoryStore) ForgetMemoryMessage(ctx context.Context, _, _ string, _ int64, _ time.Time) (bool, error) {
	close(s.entered)
	<-s.release
	return false, ctx.Err()
}

func TestMemoryForgetCanceledWriteRetainsAuthority(t *testing.T) {
	f := scratch.New(t)
	previous := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = previous })
	if err := f.DB.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.Memory{}); err != nil {
		t.Fatal(err)
	}
	p := f.Prefix
	owner, member, memoryID := p+"owner", p+"member", p+"memory"
	active := "1"
	for _, id := range []string{owner, member} {
		if err := f.DB.Create(&entity.User{ID: id, Email: id + "@example.test", Nickname: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := f.DB.Create(&entity.Tenant{ID: owner, Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Create(&entity.UserTenant{ID: p + "membership", UserID: member, TenantID: owner, Role: "normal", Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Create(&entity.Memory{ID: memoryID, TenantID: owner, Name: memoryID, EmbdID: "", LLMID: "", Permissions: "team"}).Error; err != nil {
		t.Fatal(err)
	}
	store := &drainingMemoryStore{entered: make(chan struct{}), release: make(chan struct{})}
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	done := make(chan error, 1)
	var once sync.Once
	release := func() { once.Do(func() { close(store.release) }) }
	var finished bool
	t.Cleanup(func() {
		release()
		if !finished {
			select {
			case <-done:
			case <-time.After(3 * time.Second):
				t.Error("draining memory call survived cleanup")
			}
		}
	})
	go func() { done <- NewMemoryService(store).ForgetMessage(ctx, member, memoryID, 0) }()
	select {
	case <-store.entered:
	case <-time.After(3 * time.Second):
		t.Fatal("native memory boundary not entered")
	}
	cancel()
	lockCheck, err := f.Read.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer lockCheck.Rollback()
	if _, err := lockCheck.ExecContext(t.Context(), "SET LOCAL lock_timeout='250ms'"); err != nil {
		t.Fatal(err)
	}
	_, err = lockCheck.ExecContext(t.Context(), "UPDATE t_ai_user_tenants SET role='admin' WHERE id=$1", p+"membership")
	var blocked *pgconn.PgError
	if !errors.As(err, &blocked) || blocked.Code != "55P03" {
		t.Fatal("cancellation released SQL authority before native drain", err)
	}
	if err := lockCheck.Rollback(); err != nil {
		t.Fatal(err)
	}
	release()
	select {
	case err := <-done:
		finished = true
		if !errors.Is(err, context.Canceled) {
			t.Fatal("canceled memory result", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("memory drain did not complete")
	}
	if _, err := f.Read.ExecContext(t.Context(), "UPDATE t_ai_user_tenants SET role='admin' WHERE id=$1", p+"membership"); err != nil {
		t.Fatal("SQL authority not released after native drain", err)
	}
	f.Journal.Event("memory-cancel-drain-verified", map[string]any{"member": member, "blocked_sqlstate": "55P03", "released_after_native_return": true})
}

func (s *controlledMemoryStore) ForgetMemoryMessage(ctx context.Context, tenant, memory string, id int64, at time.Time) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, err
	}
	s.calls++
	s.tenant, s.memory, s.id = tenant, memory, id
	if s.err != nil {
		return false, s.err
	}
	if s.forgot.IsZero() {
		s.forgot = at
	}
	return true, nil
}

func TestMemoryForgetScratch(t *testing.T) {
	f := scratch.New(t)
	previous := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = previous })
	if err := f.DB.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.Memory{}); err != nil {
		t.Fatal(err)
	}
	p := f.Prefix
	owner, member, invite, revoked, foreign := p+"owner", p+"member", p+"invite", p+"revoked", p+"foreign"
	active, inactive := "1", "0"
	for _, id := range []string{owner, member, invite, revoked, foreign} {
		if err := f.DB.Create(&entity.User{ID: id, Email: id + "@example.test", Nickname: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := f.DB.Create(&entity.Tenant{ID: owner, Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	for _, m := range []entity.UserTenant{{ID: p + "m", UserID: member, TenantID: owner, Role: "normal", Status: &active}, {ID: p + "i", UserID: invite, TenantID: owner, Role: "invite", Status: &active}, {ID: p + "r", UserID: revoked, TenantID: owner, Role: "admin", Status: &inactive}} {
		if err := f.DB.Create(&m).Error; err != nil {
			t.Fatal(err)
		}
	}
	memoryID := p + "memory"
	if err := f.DB.Create(&entity.Memory{ID: memoryID, TenantID: owner, Name: "memory", EmbdID: "", LLMID: "", Permissions: "team"}).Error; err != nil {
		t.Fatal(err)
	}
	store := &controlledMemoryStore{}
	s := NewMemoryService(store)
	for _, actor := range []string{owner, member} {
		if err := s.ForgetMessage(t.Context(), actor, memoryID, 0); err != nil {
			t.Fatal(err)
		}
	}
	if store.calls != 2 || store.tenant != owner || store.memory != memoryID || store.id != 0 || store.forgot.IsZero() {
		t.Fatal("wrong owner scope or zero ID")
	}
	for _, actor := range []string{invite, revoked, foreign} {
		before := store.calls
		if err := s.ForgetMessage(t.Context(), actor, memoryID, 0); !errors.Is(err, ErrMemoryUnavailable) {
			t.Fatal("unauthorized actor accepted", err)
		}
		if before != store.calls {
			t.Fatal("unauthorized store access")
		}
	}
	if err := f.DB.Model(&entity.Memory{}).Where("id=?", memoryID).Update("permissions", "me").Error; err != nil {
		t.Fatal(err)
	}
	if err := s.ForgetMessage(t.Context(), member, memoryID, 0); !errors.Is(err, ErrMemoryUnavailable) {
		t.Fatal("private memory shared", err)
	}
	store.err = errors.New("controlled store failure")
	if err := s.ForgetMessage(t.Context(), owner, memoryID, 0); !errors.Is(err, store.err) {
		t.Fatal("store failure hidden", err)
	}
	if err := NewMemoryService(nil).ForgetMessage(t.Context(), owner, memoryID, 0); !errors.Is(err, types.ErrMemoryWriteUnavailable) {
		t.Fatal("unimplemented writer acknowledged success", err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	before := store.calls
	if err := s.ForgetMessage(ctx, owner, memoryID, 0); !errors.Is(err, context.Canceled) || before != store.calls {
		t.Fatal("canceled service reached store", err)
	}
	if _, err := dao.NewMemoryDAO().GetByIDWithContext(ctx, memoryID); !errors.Is(err, context.Canceled) {
		t.Fatal("memory DAO lost context", err)
	}
	if _, err := dao.NewUserTenantDAO().GetActiveByUserIDWithContext(ctx, member); !errors.Is(err, context.Canceled) {
		t.Fatal("membership DAO lost context", err)
	}
	if err := f.DB.Model(&entity.Tenant{}).Where("id=?", owner).Update("status", "0").Error; err != nil {
		t.Fatal(err)
	}
	before = store.calls
	if err := s.ForgetMessage(t.Context(), owner, memoryID, 0); !errors.Is(err, ErrMemoryUnavailable) || before != store.calls {
		t.Fatal("inactive owner wrote to store", err)
	}
}
