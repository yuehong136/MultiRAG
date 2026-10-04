package skills

import (
	"context"
	"errors"
	"sort"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/entity"
)

func (s *Service) Batch(ctx context.Context, tenant, space, kind, key string, ids []string) (Operation, error) {
	if e := validateKey(key); e != nil {
		return Operation{}, e
	}
	if len(ids) < 1 || len(ids) > 100 {
		return Operation{}, fault(422, "INVALID_IDS")
	}
	sort.Strings(ids)
	unique := []string{}
	for _, id := range ids {
		if len(unique) == 0 || unique[len(unique)-1] != id {
			unique = append(unique, id)
		}
	}
	payload := JSON{"ids": unique, "space_id": space}
	op := makeOperation(tenant, kind, key, payload)
	if space != "" {
		op.SpaceID = &space
	}
	e := s.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		old, e := existingOperation(tx, tenant, kind, key, op.RequestHash)
		if e != nil {
			return e
		}
		if old != nil {
			op = *old
			return nil
		}
		items := []JSON{}
		eligible := []string{}
		for _, id := range unique {
			spID := space
			if kind == "delete_spaces" {
				spID = id
			}
			sp, e := spaceIn(tx.Clauses(clause.Locking{Strength: "UPDATE"}), tenant, spID, true)
			if e != nil {
				code := "NOT_FOUND"
				var fe *Error
				if errors.As(e, &fe) {
					code = fe.Code
				}
				items = append(items, JSON{"id": id, "state": "failed", "error_code": code, "retryable": false})
				continue
			}
			if kind == "delete_spaces" {
				if e = tx.Model(&Space{}).Where("id = ?", id).Updates(map[string]any{"state": "deleting", "revision": gorm.Expr("revision + 1")}).Error; e != nil {
					return e
				}
			} else {
				r := tx.Model(&Skill{}).Where("id = ? AND tenant_id = ? AND space_id = ? AND state = 'active'", id, tenant, space).Updates(map[string]any{"state": "deleting", "active_version_id": nil, "revision": gorm.Expr("revision + 1")})
				if r.Error != nil {
					return r.Error
				}
				if r.RowsAffected == 0 {
					items = append(items, JSON{"id": id, "state": "failed", "error_code": "NOT_FOUND", "retryable": false})
					continue
				}
				if e = bump(tx, sp.ID); e != nil {
					return e
				}
			}
			eligible = append(eligible, id)
		}
		op.Payload["eligible_ids"] = eligible
		op.Result = JSON{"items": items}
		op.Progress = JSON{"completed": 0, "total": len(unique)}
		return tx.Create(&op).Error
	})
	return op, e
}
func (s *Service) deleteBatch(ctx context.Context, op Operation) (JSON, error) {
	raw, _ := op.Payload["eligible_ids"].([]any)
	if strings, ok := op.Payload["eligible_ids"].([]string); ok {
		for _, id := range strings {
			raw = append(raw, id)
		}
	}
	items := []any{}
	completed := map[string]bool{}
	if old, ok := op.Result["items"].([]any); ok {
		for _, v := range old {
			j := asJSON(v)
			if j["retryable"] == false {
				items = append(items, j)
				completed[stringValue(j["id"])] = true
			}
		}
	}
	failed := false
	for _, v := range raw {
		id := stringValue(v)
		if completed[id] {
			continue
		}
		kind := "delete_skill"
		space := stringValue(op.Payload["space_id"])
		if op.Kind == "delete_spaces" {
			kind = "delete_space"
			space = id
		}
		_, e := s.deleteResource(ctx, op, kind, id, space)
		item := JSON{"id": id, "state": "succeeded", "retryable": false}
		if e != nil {
			failed = true
			code := "DELETE_FAILED"
			var fe *Error
			if errors.As(e, &fe) {
				code = fe.Code
			}
			item = JSON{"id": id, "state": "failed", "error_code": code, "retryable": true}
		}
		items = append(items, item)
	}
	for _, v := range items {
		if asJSON(v)["state"] == "failed" {
			failed = true
		}
	}
	failedCount := 0
	for _, v := range items {
		if asJSON(v)["state"] != "succeeded" {
			failedCount++
		}
	}
	return JSON{"items": items, "partial": failed, "all_failed": failedCount == len(items) && failedCount > 0}, nil
}
func (s *Service) deleteResource(ctx context.Context, op Operation, kind, id, space string) (JSON, error) {
	var sp Space
	if e := s.DB.WithContext(ctx).Where("id = ? AND tenant_id = ? AND backend_owner = 'go'", space, op.TenantID).First(&sp).Error; e != nil {
		return nil, missing(e)
	}
	if e := checkLease(s.DB.WithContext(ctx), op); e != nil {
		return nil, e
	}
	var versions []Version
	var skills []Skill
	q := s.DB.WithContext(ctx).Where("tenant_id = ? AND space_id = ?", op.TenantID, space)
	if kind != "delete_space" {
		if kind == "delete_skill" {
			q = q.Where("id = ?", id)
		} else {
			var v Version
			if e := s.DB.WithContext(ctx).Where("id = ? AND tenant_id = ?", id, op.TenantID).First(&v).Error; e != nil {
				return nil, missing(e)
			}
			q = q.Where("id = ?", v.SkillID)
		}
	}
	if e := q.Find(&skills).Error; e != nil {
		return nil, e
	}
	skillIDs := []string{}
	for _, sk := range skills {
		skillIDs = append(skillIDs, sk.ID)
	}
	if len(skillIDs) > 0 {
		q = s.DB.WithContext(ctx).Where("tenant_id = ? AND skill_id IN ? AND state <> 'deleted'", op.TenantID, skillIDs)
		if kind == "delete_version" {
			q = q.Where("id = ?", id)
		}
		if e := q.Find(&versions).Error; e != nil {
			return nil, e
		}
	}
	versionIDs := []string{}
	for _, v := range versions {
		versionIDs = append(versionIDs, v.ID)
	}
	var staging int64
	if len(versionIDs) > 0 {
		if e := s.DB.WithContext(ctx).Table(Operation{}.TableName()).Where("tenant_id = ? AND resource_id IN ? AND kind = 'install' AND phase = 'staging' AND state = 'pending'", op.TenantID, versionIDs).Count(&staging).Error; e != nil {
			return nil, e
		}
		if staging > 0 {
			return nil, fault(409, "UPLOAD_IN_PROGRESS")
		}
	}
	fail := func(e error) (JSON, error) {
		model := any(&Version{})
		if kind == "delete_space" {
			model = &Space{}
		} else if kind == "delete_skill" {
			model = &Skill{}
		}
		fenced(s.DB.WithContext(context.WithoutCancel(ctx)), op, func(tx *gorm.DB) error {
			return tx.Model(model).Where("id = ? AND state <> 'deleted'", id).Update("state", "delete_failed").Error
		})
		return nil, e
	}
	// Mark descendants before physical work so cross-backend readers cannot observe them.
	if e := fenced(s.DB.WithContext(ctx), op, func(tx *gorm.DB) error {
		if len(versionIDs) > 0 {
			if e := tx.Model(&Version{}).Where("id IN ?", versionIDs).Update("state", "deleting").Error; e != nil {
				return e
			}
		}
		if kind != "delete_version" && len(skillIDs) > 0 {
			return tx.Model(&Skill{}).Where("id IN ? AND state <> 'deleted'", skillIDs).Updates(map[string]any{"state": "deleting", "active_version_id": nil}).Error
		}
		return nil
	}); e != nil {
		return nil, e
	}
	var gs []Generation
	if e := s.DB.WithContext(ctx).Where("tenant_id = ? AND space_id = ? AND state <> 'deleted'", op.TenantID, space).Find(&gs).Error; e != nil {
		return fail(e)
	}
	if len(gs) > 0 && s.Index == nil {
		return fail(fault(503, "SEARCH_UNAVAILABLE"))
	}
	for _, g := range gs {
		if e := checkLease(s.DB.WithContext(ctx), op); e != nil {
			return nil, e
		}
		var e error
		if kind == "delete_space" {
			e = s.Index.Drop(ctx, g.IndexName)
		} else {
			e = s.Index.Delete(ctx, g.IndexName, versionIDs)
		}
		if e != nil {
			return fail(fault(503, "INDEX_DELETE_FAILED"))
		}
		if kind == "delete_space" {
			if e = s.DB.WithContext(ctx).Model(&Generation{}).Where("id = ?", g.ID).Update("state", "deleted").Error; e != nil {
				return fail(e)
			}
		}
	}
	for _, v := range versions {
		if e := s.cleanVersion(ctx, op, v); e != nil {
			return fail(e)
		}
	}
	if kind != "delete_version" {
		for _, sk := range skills {
			if sk.State == "deleted" {
				continue
			}
			if e := s.deleteFolder(ctx, op, sk.FolderID); e != nil {
				return fail(e)
			}
			if e := fenced(s.DB.WithContext(ctx), op, func(tx *gorm.DB) error {
				return tx.Model(&Skill{}).Where("id = ?", sk.ID).Updates(map[string]any{"state": "deleted", "deleted_at": time.Now().UTC(), "active_version_id": nil}).Error
			}); e != nil {
				return nil, e
			}
		}
	}
	if kind == "delete_space" {
		if e := s.deleteFolder(ctx, op, sp.RootFolderID); e != nil {
			return fail(e)
		}
		if e := fenced(s.DB.WithContext(ctx), op, func(tx *gorm.DB) error {
			return tx.Model(&Space{}).Where("id = ?", space).Updates(map[string]any{"state": "deleted", "deleted_at": time.Now().UTC(), "active_generation_id": nil}).Error
		}); e != nil {
			return nil, e
		}
	}
	return JSON{"items": []JSON{{"id": id, "state": "succeeded", "retryable": false}}}, nil
}
func (s *Service) cleanVersion(ctx context.Context, op Operation, v Version) error {
	var bindings []VersionFile
	if e := s.DB.WithContext(ctx).Where("tenant_id = ? AND version_id = ?", op.TenantID, v.ID).Find(&bindings).Error; e != nil {
		return e
	}
	// Install operations retain intended immutable addresses after File rows are removed.
	var install Operation
	query := s.DB.WithContext(ctx).Where("tenant_id = ? AND kind = 'install' AND resource_id = ?", op.TenantID, v.ID).Order("create_time,id").First(&install)
	if query.Error != nil && !errors.Is(query.Error, gorm.ErrRecordNotFound) {
		return query.Error
	}
	objects, _ := install.Payload["objects"].([]any)
	if len(objects) > 0 && s.Blobs == nil {
		return fault(503, "STORAGE_UNAVAILABLE")
	}
	for _, raw := range objects {
		object := asJSON(raw)
		bucket, key := stringValue(object["bucket"]), stringValue(object["key"])
		if bucket == "" || key == "" {
			return fault(503, "CONTENT_INTEGRITY")
		}
		if e := checkLease(s.DB.WithContext(ctx), op); e != nil {
			return e
		}
		if e := s.Blobs.Remove(bucket, key); e != nil {
			return fault(503, "STORAGE_DELETE_FAILED")
		}
		if exists, e := s.Blobs.Exists(bucket, key); e != nil || exists {
			return fault(503, "STORAGE_DELETE_FAILED")
		}
	}
	if len(bindings) > 0 && s.Blobs == nil {
		return fault(503, "STORAGE_UNAVAILABLE")
	}
	for _, b := range bindings {
		if e := checkLease(s.DB.WithContext(ctx), op); e != nil {
			return e
		}
		var file entity.File
		if e := s.DB.WithContext(ctx).Where("id = ? AND tenant_id = ?", b.FileID, op.TenantID).First(&file).Error; e != nil {
			return e
		}
		if file.Location == nil {
			return fault(503, "STORAGE_UNAVAILABLE")
		}
		if e := s.Blobs.Remove(file.ParentID, *file.Location); e != nil {
			return fault(503, "STORAGE_DELETE_FAILED")
		}
		if exists, e := s.Blobs.Exists(file.ParentID, *file.Location); e != nil || exists {
			return fault(503, "STORAGE_DELETE_FAILED")
		}
		if e := fenced(s.DB.WithContext(ctx), op, func(tx *gorm.DB) error {
			if e := tx.Delete(&VersionFile{}, "id = ?", b.ID).Error; e != nil {
				return e
			}
			return tx.Delete(&entity.File{}, "id = ? AND tenant_id = ?", file.ID, op.TenantID).Error
		}); e != nil {
			return e
		}
	}
	if e := s.deleteFolder(ctx, op, v.FolderID); e != nil {
		return e
	}
	return fenced(s.DB.WithContext(ctx), op, func(tx *gorm.DB) error {
		return tx.Model(&Version{}).Where("id = ?", v.ID).Updates(map[string]any{"state": "deleted", "deleted_at": time.Now().UTC(), "index_state": "unindexed"}).Error
	})
}
func (s *Service) deleteFolder(ctx context.Context, op Operation, id string) error {
	var children []entity.File
	if e := s.DB.WithContext(ctx).Where("tenant_id = ? AND parent_id = ? AND id <> ?", op.TenantID, id, id).Find(&children).Error; e != nil {
		return e
	}
	for _, child := range children {
		if child.Type != "folder" || child.SourceType != "skill_file" {
			return fault(409, "UNEXPECTED_CHILD")
		}
		if e := s.deleteFolder(ctx, op, child.ID); e != nil {
			return e
		}
	}
	return fenced(s.DB.WithContext(ctx), op, func(tx *gorm.DB) error {
		var count int64
		if e := tx.Model(&entity.File{}).Where("tenant_id = ? AND parent_id = ? AND id <> ?", op.TenantID, id, id).Count(&count).Error; e != nil {
			return e
		}
		if count > 0 {
			return fault(409, "UNEXPECTED_CHILD")
		}
		return tx.Delete(&entity.File{}, "id = ? AND tenant_id = ?", id, op.TenantID).Error
	})
}
