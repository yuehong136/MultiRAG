package service

import (
	"context"
	"errors"
	"fmt"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
)

var ErrMemoryUnavailable = errors.New("memory is unavailable or unauthorized")

func (s *MemoryService) ForgetMessage(ctx context.Context, userID, memoryID string, messageID int64) error {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return err
	}
	if _, _, err := types.MemoryMessageAddress(userID, memoryID, messageID); err != nil {
		return ErrMemoryUnavailable
	}
	if dao.DB == nil {
		return fmt.Errorf("memory SQL unavailable")
	}
	store := s.messageStore
	if store == nil {
		store, _ = engine.Get().(types.MemoryMessageStore)
	}
	// Acquisition and authorization reads honor the request. The transaction's
	// lifetime must outlive cancellation once an uncancellable native RPC has
	// started: database/sql otherwise rolls it back and drops authority locks
	// while that RPC can still write. Return the pinned connection only after
	// the store has drained its write/readback/close.
	return dao.DB.WithContext(ctx).Connection(func(conn *gorm.DB) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		return conn.Session(&gorm.Session{NewDB: true, Context: context.WithoutCancel(ctx)}).Transaction(func(tx *gorm.DB) error {
			tx = tx.WithContext(ctx)
			var memory entity.Memory
			if err := tx.Clauses(clause.Locking{Strength: "SHARE"}).Where("id=?", memoryID).Take(&memory).Error; err != nil {
				if errors.Is(err, gorm.ErrRecordNotFound) {
					return ErrMemoryUnavailable
				}
				return err
			}
			if memory.TenantID != userID && memory.Permissions != "team" {
				return ErrMemoryUnavailable
			}
			// Hold current actor/tenant/membership authority through the external
			// write. Invitations, revoked members and inactive owners fail closed.
			if err := authorizeLinkTenant(tx, userID, memory.TenantID); err != nil {
				if errors.Is(err, ErrLinkUnavailable) {
					return ErrMemoryUnavailable
				}
				return err
			}
			if store == nil {
				return types.ErrMemoryWriteUnavailable
			}
			_, err := store.ForgetMemoryMessage(ctx, memory.TenantID, memory.ID, messageID, time.Now().UTC())
			if err != nil {
				return err
			}
			return ctx.Err()
		})
	})
}
