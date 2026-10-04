package service

import (
	"context"
	"fmt"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/entity"
	"time"
)

// RecoverDeletes resumes persisted space tombstones after process termination.
func (s *SkillSpaceService) RecoverDeletes(ctx context.Context, doc engine.SkillDocEngine) {
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	for {
		var spaces []entity.SkillSpace
		if dao.DB.WithContext(ctx).Where("status IN ?", []string{entity.SpaceStatusDeleting, entity.SpaceStatusDeleteFailed}).Find(&spaces).Error == nil {
			for _, space := range spaces {
				if ctx.Err() != nil {
					return
				}
				s.asyncDeleteSpace(space.ID, space.FolderID, space.TenantID, doc, ctx)
			}
		}
		var pending []entity.SkillSpace
		if dao.DB.WithContext(ctx).Where("(status = '1' AND core_state->'pending_delete' IS NOT NULL) OR core_state->'pending_upload' IS NOT NULL OR core_state->'upload_tombstones' IS NOT NULL").Find(&pending).Error == nil {
			for _, space := range pending {
				db, e := dao.DB.DB()
				if e != nil {
					continue
				}
				conn, e := db.Conn(ctx)
				if e != nil {
					continue
				}
				key := "go-skills:" + space.TenantID + ":" + space.ID
				if _, e = conn.ExecContext(ctx, "SELECT pg_advisory_lock(hashtextextended($1,0))", key); e == nil {
					if dao.DB.First(&space, "id = ?", space.ID).Error == nil {
						if space.CoreState["pending_upload"] != nil || space.CoreState["upload_tombstones"] != nil {
							_ = recoverCoreUploads(ctx, &space)
						}
						if space.CoreState["pending_delete"] != nil {
							_ = resumeCoreFiles(ctx, &space)
						}
					}
					_, _ = conn.ExecContext(context.Background(), "SELECT pg_advisory_unlock(hashtextextended($1,0))", key)
				}
				conn.Close()
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// deleteCoreTree removes storage before its durable File address. A provider
// failure leaves the row intact for the next recovery pass.
func deleteCoreTree(ctx context.Context, tenant, id string) error {
	var row entity.File
	if err := dao.DB.WithContext(ctx).Where("id = ? AND tenant_id = ?", id, tenant).Find(&row).Error; err != nil {
		return err
	}
	if row.ID == "" {
		return nil
	}
	var children []entity.File
	if err := dao.DB.WithContext(ctx).Where("parent_id = ? AND id <> ? AND tenant_id = ?", id, id, tenant).Find(&children).Error; err != nil {
		return err
	}
	for _, child := range children {
		if err := deleteCoreTree(ctx, tenant, child.ID); err != nil {
			return err
		}
	}
	if row.Location != nil && *row.Location != "" {
		_, store := skillCoreResources()
		if store == nil {
			return fmt.Errorf("storage unavailable")
		}
		if err := store.Remove(row.ParentID, *row.Location); err != nil {
			return err
		}
	}
	return dao.DB.WithContext(ctx).Where("id = ? AND tenant_id = ?", id, tenant).Delete(&entity.File{}).Error
}
