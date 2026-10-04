package skills

import (
	"context"
	"errors"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/entity"
)

const leaseSeconds = 60

// Claim uses database time and a fencing revision, including for crashed workers.
func (s *Service) Claim(ctx context.Context, owner string) (*Operation, error) {
	var op Operation
	e := s.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		e := tx.Clauses(clause.Locking{Strength: "UPDATE", Options: "SKIP LOCKED"}).Where("backend_owner = 'go' AND phase <> 'staging' AND ((state = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= clock_timestamp())) OR (state = 'running' AND lease_expires_at <= clock_timestamp()))").Order("create_time,id").First(&op).Error
		if e != nil {
			return e
		}
		r := tx.Table(Operation{}.TableName()).Where("id = ?", op.ID).Updates(map[string]any{"state": "running", "attempts": gorm.Expr("attempts + 1"), "revision": gorm.Expr("revision + 1"), "lease_owner": owner, "lease_expires_at": gorm.Expr("clock_timestamp() + INTERVAL '60 seconds'")})
		if r.Error != nil {
			return r.Error
		}
		return tx.First(&op, "id = ?", op.ID).Error
	})
	if errors.Is(e, gorm.ErrRecordNotFound) {
		return nil, nil
	}
	return &op, e
}
func leaseQuery(db *gorm.DB, op Operation) *gorm.DB {
	return db.Table(Operation{}.TableName()).Where("id = ? AND backend_owner = 'go' AND state = 'running' AND lease_owner = ? AND revision = ? AND lease_expires_at > clock_timestamp()", op.ID, op.LeaseOwner, op.Revision)
}
func checkLease(db *gorm.DB, op Operation) error {
	var count int64
	e := leaseQuery(db, op).Count(&count).Error
	if e != nil {
		return e
	}
	if count != 1 {
		return fault(409, "LEASE_LOST")
	}
	return nil
}
func fenced(db *gorm.DB, op Operation, fn func(*gorm.DB) error) error {
	return db.Transaction(func(tx *gorm.DB) error {
		var row Operation
		e := leaseQuery(tx.Clauses(clause.Locking{Strength: "UPDATE"}), op).First(&row).Error
		if e != nil {
			return fault(409, "LEASE_LOST")
		}
		if e = checkLease(tx, op); e != nil {
			return e
		}
		return fn(tx)
	})
}
func (s *Service) heartbeat(ctx context.Context, op Operation) error {
	r := leaseQuery(s.DB.WithContext(ctx), op).Update("lease_expires_at", gorm.Expr("clock_timestamp() + INTERVAL '60 seconds'"))
	if r.Error != nil {
		return r.Error
	}
	if r.RowsAffected != 1 {
		return fault(409, "LEASE_LOST")
	}
	return nil
}
func (s *Service) Run(ctx context.Context) {
	owner := newID()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if s.Ready(ctx) != nil {
				continue
			}
			op, e := s.Claim(ctx, owner)
			if e == nil && op != nil {
				s.Execute(ctx, *op)
			}
			s.recoverStaging(ctx)
			s.cleanRetired(ctx)
		}
	}
}
func (s *Service) Execute(ctx context.Context, op Operation) error {
	work, cancel := context.WithCancel(ctx)
	defer cancel()
	done := make(chan struct{})
	go func() {
		t := time.NewTicker(15 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-work.Done():
				return
			case <-done:
				return
			case <-t.C:
				if s.heartbeat(work, op) != nil {
					cancel()
					return
				}
			}
		}
	}()
	result, e := s.execute(work, op)
	close(done)
	if work.Err() != nil {
		return work.Err()
	}
	return fenced(s.DB.WithContext(ctx), op, func(tx *gorm.DB) error {
		u := stamp()
		u["lease_owner"] = nil
		u["lease_expires_at"] = nil
		if e != nil {
			code := "OPERATION_FAILED"
			var fe *Error
			if errors.As(e, &fe) {
				code = fe.Code
			}
			u["state"] = "failed"
			u["error"] = JSON{"error_code": code, "message": code, "retryable": code != "INCOMPLETE_UPLOAD"}
			if code == "SOURCE_CHANGED" {
				u["state"] = "pending"
				u["next_attempt_at"] = gorm.Expr("clock_timestamp() + INTERVAL '1 second'")
			}
		} else {
			u["state"] = "succeeded"
			if result["partial"] == true {
				u["state"] = "partial"
				delete(result, "partial")
			}
			if result["all_failed"] == true {
				u["state"] = "failed"
			}
			delete(result, "all_failed")
			u["phase"] = "done"
			u["result"] = result
			total, completed := 1, 1
			switch items := result["items"].(type) {
			case []any:
				total = len(items)
				completed = 0
				for _, item := range items {
					if asJSON(item)["state"] == "succeeded" {
						completed++
					}
				}
			case []JSON:
				total = len(items)
				completed = 0
				for _, item := range items {
					if item["state"] == "succeeded" {
						completed++
					}
				}
			}
			u["progress"] = JSON{"completed": completed, "total": total}
			u["error"] = nil
		}
		return leaseQuery(tx, op).Updates(u).Error
	})
}
func (s *Service) execute(ctx context.Context, op Operation) (JSON, error) {
	if op.Payload["incomplete_upload"] == true {
		var v Version
		if e := s.DB.WithContext(ctx).Where("id = ? AND tenant_id = ?", op.ResourceID, op.TenantID).First(&v).Error; e != nil {
			return nil, e
		}
		if e := s.cleanVersion(ctx, op, v); e != nil {
			return nil, e
		}
		return nil, fault(409, "INCOMPLETE_UPLOAD")
	}
	if op.Phase == "cleaning" && (op.Kind == "install" || op.Kind == "activate" || op.Kind == "reindex") {
		if e := s.cleanSpaceGenerations(ctx, op, stringValue(op.Payload["space_id"])); e != nil {
			return nil, e
		}
		return op.Result, nil
	}
	if old := stringValue(op.Payload["generation_id"]); old != "" {
		if e := s.DB.WithContext(ctx).Model(&Generation{}).Where("id = ? AND state = 'building'", old).Update("state", "failed").Error; e != nil {
			return nil, e
		}
	}
	switch op.Kind {
	case "install":
		if op.Payload["activate"] == true {
			return s.build(ctx, op, stringValue(op.Payload["skill_id"]), stringValue(op.Payload["version_id"]))
		}
		return JSON{"items": []JSON{{"id": op.ResourceID, "state": "succeeded", "retryable": false}}, "skill_id": op.Payload["skill_id"], "version_id": op.Payload["version_id"], "index_state": "unindexed", "skipped_binary_count": op.Payload["skipped_binary_count"]}, nil
	case "activate":
		return s.build(ctx, op, stringValue(op.Payload["resource_id"]), stringValue(op.Payload["version_id"]))
	case "reindex":
		return s.build(ctx, op, "", "")
	case "delete_version", "delete_skill", "delete_space":
		return s.deleteResource(ctx, op, op.Kind, stringValue(op.Payload["resource_id"]), stringValue(op.Payload["space_id"]))
	case "delete_spaces", "delete_skills":
		return s.deleteBatch(ctx, op)
	}
	return nil, fault(422, "INVALID_OPERATION")
}
func (s *Service) build(ctx context.Context, op Operation, skillID, versionID string) (JSON, error) {
	space := stringValue(op.Payload["space_id"])
	sp, e := s.Space(ctx, op.TenantID, space)
	if e != nil {
		return nil, e
	}
	cfg, e := s.Config(ctx, op.TenantID, space)
	if e != nil {
		return nil, e
	}
	var skills []Skill
	if e = s.DB.WithContext(ctx).Where("tenant_id = ? AND space_id = ? AND state = 'active'", op.TenantID, space).Order("id").Find(&skills).Error; e != nil {
		return nil, e
	}
	var candidate *Skill
	ids := []string{}
	for i := range skills {
		sk := &skills[i]
		if sk.ID == skillID {
			candidate = sk
			if versionID == "" {
				sk.ActiveVersionID = nil
			} else {
				sk.ActiveVersionID = &versionID
			}
		}
		if sk.ActiveVersionID != nil {
			ids = append(ids, *sk.ActiveVersionID)
		}
	}
	if skillID != "" && candidate == nil {
		return nil, fault(404, "NOT_FOUND")
	}
	var versions []Version
	if len(ids) > 0 {
		if e = s.DB.WithContext(ctx).Where("tenant_id = ? AND id IN ? AND state = 'installed'", op.TenantID, ids).Find(&versions).Error; e != nil {
			return nil, e
		}
		if len(versions) != len(ids) {
			return nil, fault(409, "SOURCE_CHANGED")
		}
	}
	// Candidate metadata is read from its immutable package, never inherited from the old active version.
	var description string
	tags := []string{}
	if candidate != nil && versionID != "" {
		var binding VersionFile
		if e = s.DB.WithContext(ctx).Where("tenant_id = ? AND version_id = ? AND relative_path = 'SKILL.md'", op.TenantID, versionID).First(&binding).Error; e != nil {
			return nil, e
		}
		data, e := s.readFile(ctx, op.TenantID, binding)
		if e != nil {
			return nil, e
		}
		description, tags, e = metadata(data, candidate.Name)
		if e != nil {
			return nil, e
		}
	}
	var generation *Generation
	if cfg.EmbeddingModelID != nil {
		if s.Index == nil {
			return nil, fault(503, "SEARCH_UNAVAILABLE")
		}
		g := Generation{ID: newID(), TenantID: op.TenantID, SpaceID: space, ConfigRevision: cfg.Revision, SourceRevision: sp.Revision, Config: cfg.Snapshot(), State: "building", Dimension: 0, Times: nowTimes()}
		g.IndexName = "skill_" + g.ID
		e = fenced(s.DB.WithContext(ctx), op, func(tx *gorm.DB) error {
			if e = tx.Create(&g).Error; e != nil {
				return e
			}
			op.Payload["generation_id"] = g.ID
			return leaseQuery(tx, op).Updates(map[string]any{"phase": "indexing", "payload": gorm.Expr("jsonb_set(payload, '{generation_id}', to_jsonb(?::text))", g.ID)}).Error
		})
		if e != nil {
			return nil, e
		}
		generation = &g
		rows, dim, e := s.indexRows(ctx, op.TenantID, cfg, skills, versions)
		if e == nil {
			e = checkLease(s.DB.WithContext(ctx), op)
		}
		if e == nil {
			var current Space
			current, e = s.Space(ctx, op.TenantID, space)
			if e == nil && current.Revision != sp.Revision {
				e = fault(409, "SOURCE_CHANGED")
			}
		}
		if e == nil {
			e = s.Index.Build(ctx, g.IndexName, dim, rows)
		}
		if e != nil {
			s.DB.WithContext(context.WithoutCancel(ctx)).Model(&Generation{}).Where("id = ? AND state = 'building'", g.ID).Updates(map[string]any{"state": "failed", "error": JSON{"error_code": "INDEX_BUILD_FAILED"}})
			return nil, e
		}
		g.Dimension = dim
	} else if op.Kind == "reindex" {
		return nil, fault(503, "MODEL_NOT_CONFIGURED")
	}
	e = fenced(s.DB.WithContext(ctx), op, func(tx *gorm.DB) error {
		var current Space
		if e := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ? AND tenant_id = ? AND backend_owner = 'go' AND state = 'active' AND revision = ?", space, op.TenantID, sp.Revision).First(&current).Error; e != nil {
			return fault(409, "SOURCE_CHANGED")
		}
		if candidate != nil {
			updates := stamp()
			updates["active_version_id"] = candidate.ActiveVersionID
			updates["revision"] = gorm.Expr("revision + 1")
			if versionID != "" {
				updates["description"] = description
				tagJSON := entity.JSONSlice{}
				for _, tag := range tags {
					tagJSON = append(tagJSON, tag)
				}
				updates["tags"] = tagJSON
			}
			query := tx.Model(&Skill{}).Where("id = ? AND state = 'active'", candidate.ID)
			if op.Kind == "activate" {
				revision, _ := number(op.Payload["revision"])
				query = query.Where("revision = ?", int64(revision))
			}
			r := query.Updates(updates)
			if r.Error != nil {
				return r.Error
			}
			if r.RowsAffected != 1 {
				return fault(409, "REVISION_CONFLICT")
			}
		}
		u := stamp()
		u["revision"] = gorm.Expr("revision + 1")
		u["active_generation_id"] = nil
		if generation == nil && versionID != "" {
			if e := tx.Model(&Version{}).Where("id = ?", versionID).Update("index_state", "unindexed").Error; e != nil {
				return e
			}
		}
		if generation != nil {
			u["active_generation_id"] = generation.ID
			if e := tx.Model(&Generation{}).Where("id = ? AND state = 'building'", generation.ID).Updates(map[string]any{"state": "active", "dimension": generation.Dimension}).Error; e != nil {
				return e
			}
			if len(ids) > 0 {
				if e := tx.Model(&Version{}).Where("id IN ?", ids).Update("index_state", "ready").Error; e != nil {
					return e
				}
			}
		}
		if current.ActiveGenerationID != nil {
			if e := tx.Model(&Generation{}).Where("id = ?", *current.ActiveGenerationID).Update("state", "retired").Error; e != nil {
				return e
			}
		}
		if e := tx.Model(&Space{}).Where("id = ?", space).Updates(u).Error; e != nil {
			return e
		}
		state := "unindexed"
		if generation != nil {
			state = "ready"
		}
		return leaseQuery(tx, op).Updates(map[string]any{"phase": "cleaning", "result": JSON{"items": []JSON{{"id": op.ResourceID, "state": "succeeded", "retryable": false}}, "skill_id": skillID, "version_id": versionID, "index_state": state, "skipped_binary_count": op.Payload["skipped_binary_count"]}}).Error
	})
	if e != nil {
		if generation != nil {
			s.DB.WithContext(context.WithoutCancel(ctx)).Model(&Generation{}).Where("id = ? AND state = 'building'", generation.ID).Update("state", "failed")
		}
		return nil, e
	}
	if e := s.cleanSpaceGenerations(ctx, op, space); e != nil {
		return nil, e
	}
	state := "unindexed"
	if generation != nil {
		state = "ready"
	}
	return JSON{"items": []JSON{{"id": op.ResourceID, "state": "succeeded", "retryable": false}}, "skill_id": skillID, "version_id": versionID, "index_state": state, "skipped_binary_count": op.Payload["skipped_binary_count"]}, nil
}

// Retired/failed generations are their own durable cleanup records; publishing is never repeated to retry cleanup.
func (s *Service) cleanRetired(ctx context.Context) {
	// A failed database write after external build can leave the generation building.
	// Never collect a generation held by a queued operation or a live lease.
	if e := s.DB.WithContext(ctx).Exec(`UPDATE t_ai_skill_index_generations g SET state='failed'
        FROM t_ai_skill_spaces s WHERE s.id=g.space_id AND s.backend_owner='go' AND g.state='building'
        AND NOT EXISTS (SELECT 1 FROM t_ai_skill_operations o WHERE o.payload->>'generation_id'=g.id
          AND o.backend_owner='go' AND (o.state='pending' OR (o.state='running' AND o.lease_expires_at > clock_timestamp())))`).Error; e != nil {
		return
	}
	if s.Index == nil {
		return
	}
	var gs []Generation
	e := s.DB.WithContext(ctx).Joins("JOIN t_ai_skill_spaces s ON s.id = t_ai_skill_index_generations.space_id").Where("s.backend_owner = 'go' AND (t_ai_skill_index_generations.state IN ? OR (t_ai_skill_index_generations.state = 'deleted' AND t_ai_skill_index_generations.update_date < clock_timestamp() - INTERVAL '1 minute'))", []string{"retired", "failed", "cleanup_failed"}).Order("t_ai_skill_index_generations.update_time,t_ai_skill_index_generations.id").Limit(10).Find(&gs).Error
	if e != nil {
		return
	}
	for _, g := range gs {
		state := "deleted"
		var failure any
		if e = s.Index.Drop(ctx, g.IndexName); e != nil {
			state = "cleanup_failed"
			failure = JSON{"error_code": "INDEX_CLEANUP_FAILED"}
		}
		s.DB.WithContext(ctx).Model(&Generation{}).Where("id = ? AND state IN ?", g.ID, []string{"retired", "failed", "cleanup_failed", "deleted"}).Updates(map[string]any{"state": state, "error": failure, "update_date": time.Now().UTC(), "update_time": time.Now().UnixMilli()})
	}
}
func (s *Service) recoverStaging(ctx context.Context) {
	var ops []Operation
	if s.DB.WithContext(ctx).Where("backend_owner = 'go' AND phase = 'staging' AND (state = 'failed' OR (state = 'pending' AND update_date < clock_timestamp() - INTERVAL '1 hour'))").Limit(10).Find(&ops).Error != nil {
		return
	}
	for _, op := range ops {
		s.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
			r := tx.Table(Operation{}.TableName()).Where("id = ? AND phase = 'staging' AND (state = 'failed' OR (state = 'pending' AND update_date < clock_timestamp() - INTERVAL '1 hour'))", op.ID).Updates(map[string]any{"state": "pending", "phase": "sealed", "payload": gorm.Expr("jsonb_set(payload, '{incomplete_upload}', 'true'::jsonb)"), "error": JSON{"error_code": "INCOMPLETE_UPLOAD", "message": "INCOMPLETE_UPLOAD", "retryable": false}})
			if r.Error != nil || r.RowsAffected == 0 {
				return r.Error
			}
			return tx.Model(&Version{}).Where("id = ? AND state = 'staging'", op.ResourceID).Update("state", "install_failed").Error
		})
	}
}

func (s *Service) cleanSpaceGenerations(ctx context.Context, op Operation, space string) error {
	var gs []Generation
	if e := s.DB.WithContext(ctx).Where("tenant_id = ? AND space_id = ? AND state IN ?", op.TenantID, space, []string{"retired", "failed", "cleanup_failed"}).Find(&gs).Error; e != nil {
		return e
	}
	if len(gs) > 0 && s.Index == nil {
		return fault(503, "SEARCH_UNAVAILABLE")
	}
	for _, g := range gs {
		if e := checkLease(s.DB.WithContext(ctx), op); e != nil {
			return e
		}
		e := s.Index.Drop(ctx, g.IndexName)
		state := "deleted"
		var reason any
		if e != nil {
			state = "cleanup_failed"
			reason = JSON{"error_code": "INDEX_CLEANUP_FAILED"}
		}
		if update := fenced(s.DB.WithContext(ctx), op, func(tx *gorm.DB) error {
			return tx.Model(&Generation{}).Where("id = ? AND state IN ?", g.ID, []string{"retired", "failed", "cleanup_failed"}).Updates(map[string]any{"state": state, "error": reason}).Error
		}); update != nil {
			return update
		}
		if e != nil {
			return fault(503, "INDEX_CLEANUP_FAILED")
		}
	}
	return nil
}
