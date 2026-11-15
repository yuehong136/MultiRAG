package service

import (
	"context"
	"errors"
	"fmt"
	"multirag/internal/engine"
	"strings"
	"testing"
	"time"

	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/testsupport/scratch"
)

type controlledMemoryStore struct {
	engine.DocEngine
	calls          int
	tenant, memory string
	id             int64
	forgot         time.Time
	err            error
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
	s := NewMemoryService()
	s.docEngine = store
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
		if err := s.ForgetMessage(t.Context(), actor, memoryID, 0); !consumerMemoryMissing(err) {
			t.Fatal("unauthorized actor accepted", err)
		}
		if before != store.calls {
			t.Fatal("unauthorized store access")
		}
	}
	if err := f.DB.Model(&entity.Memory{}).Where("id=?", memoryID).Update("permissions", "me").Error; err != nil {
		t.Fatal(err)
	}
	if err := s.ForgetMessage(t.Context(), member, memoryID, 0); !consumerMemoryMissing(err) {
		t.Fatal("private memory shared", err)
	}
	store.err = errors.New("controlled store failure")
	if err := s.ForgetMessage(t.Context(), owner, memoryID, 0); !errors.Is(err, store.err) {
		t.Fatal("store failure hidden", err)
	}
	empty := NewMemoryService()
	empty.docEngine = nil
	if err := empty.ForgetMessage(t.Context(), owner, memoryID, 0); err == nil {
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
	if _, err := dao.NewUserTenantDAO().GetByUserIDWithContext(ctx, member); !errors.Is(err, context.Canceled) {
		t.Fatal("membership DAO lost context", err)
	}
	if err := f.DB.Model(&entity.Tenant{}).Where("id=?", owner).Update("status", "0").Error; err != nil {
		t.Fatal(err)
	}
	before = store.calls
	if err := s.ForgetMessage(t.Context(), owner, memoryID, 0); !consumerMemoryMissing(err) || before != store.calls {
		t.Fatal("inactive owner wrote to store", err)
	}
}

func consumerMemoryMissing(err error) bool {
	var missing *ResourceNotFoundError
	return errors.As(err, &missing)
}
func (s *controlledMemoryStore) UpdateChunks(ctx context.Context, condition map[string]interface{}, updates map[string]interface{}, index, memory string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	s.calls++
	s.memory = memory
	s.tenant = strings.TrimPrefix(index, "memory_")
	s.forgot = time.UnixMilli(updates["forget_at_flt"].(int64))
	s.id = 0
	if condition["id"] != memory+"_0" {
		return fmt.Errorf("incorrect message ID")
	}
	return s.err
}
