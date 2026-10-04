package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"gorm.io/gorm"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/entity"
	"sync"
)

type SkillBlobStore interface {
	Put(string, string, []byte, ...string) error
	Get(string, string, ...string) ([]byte, error)
	Remove(string, string, ...string) error
}

var coreRuntime struct {
	sync.RWMutex
	engine engine.SkillDocEngine
	blobs  SkillBlobStore
}

func SetSkillCoreResources(doc engine.SkillDocEngine, blobs SkillBlobStore) {
	coreRuntime.Lock()
	defer coreRuntime.Unlock()
	coreRuntime.engine = doc
	coreRuntime.blobs = blobs
}
func skillCoreResources() (engine.SkillDocEngine, SkillBlobStore) {
	coreRuntime.RLock()
	defer coreRuntime.RUnlock()
	return coreRuntime.engine, coreRuntime.blobs
}

type pendingCoreFile struct {
	ID          string `json:"id"`
	SkillFolder string `json:"skill_folder"`
	SkillName   string `json:"skill_name"`
}

func deleteSkillCoreFiles(ctx context.Context, tenant string, ids []string) (bool, error) {
	if len(ids) == 0 {
		return false, nil
	}
	space, e := dao.FindSkillCoreSpace(ids[0])
	if e != nil {
		return false, e
	}
	if space == nil {
		// A pending plan may already have removed this File before reindex failed.
		var pendingSpace entity.SkillSpace
		probe, _ := json.Marshal(map[string]any{"pending_delete": []map[string]string{{"id": ids[0]}}})
		if dao.DB.Migrator().HasTable(&entity.SkillSpace{}) {
			if e = dao.DB.Where("tenant_id=? AND status='1' AND core_state @> ?::jsonb", tenant, string(probe)).Limit(1).Find(&pendingSpace).Error; e != nil {
				return true, e
			}
			if pendingSpace.ID != "" {
				space = &pendingSpace
			}
		}
	}
	if space == nil {
		for _, id := range ids[1:] {
			other, err := dao.FindSkillCoreSpace(id)
			if err != nil {
				return true, err
			}
			if other != nil {
				return true, fmt.Errorf("mixed regular/core deletion unsupported")
			}
		}
		return false, nil
	}
	if space.TenantID != tenant {
		return true, fmt.Errorf("not found")
	}
	db, e := dao.DB.DB()
	if e != nil {
		return true, e
	}
	conn, e := db.Conn(ctx)
	if e != nil {
		return true, e
	}
	defer conn.Close()
	key := "go-skills:" + tenant + ":" + space.ID
	if _, e = conn.ExecContext(ctx, "SELECT pg_advisory_lock(hashtextextended($1,0))", key); e != nil {
		return true, e
	}
	defer conn.ExecContext(context.Background(), "SELECT pg_advisory_unlock(hashtextextended($1,0))", key)
	space, e = dao.NewSkillSpaceDAO().GetByIDAnyStatus(space.ID)
	if e != nil {
		return true, e
	}
	if space.Status != entity.SpaceStatusActive {
		return true, fmt.Errorf("space not active")
	}
	if existing := space.CoreState["pending_delete"]; existing != nil {
		raw, _ := json.Marshal(existing)
		var previous []pendingCoreFile
		if e = json.Unmarshal(raw, &previous); e != nil {
			return true, e
		}
		covered := map[string]bool{}
		for _, item := range previous {
			covered[item.ID] = true
		}
		for _, id := range ids {
			if !covered[id] {
				return true, fmt.Errorf("SKILL_CLEANUP_PENDING: retry the pending deletion before deleting other files")
			}
		}
		return true, resumeCoreFiles(ctx, space)
	}
	if space.CoreState["pending_upload"] != nil {
		return true, fmt.Errorf("SKILL_CLEANUP_PENDING: upload cleanup pending")
	}
	pending := []pendingCoreFile{}
	for _, id := range ids {
		owner, e := dao.FindSkillCoreSpace(id)
		if e != nil {
			return true, e
		}
		if owner == nil || owner.ID != space.ID {
			return true, fmt.Errorf("mixed skill spaces unsupported")
		}
		if id == space.FolderID {
			return true, fmt.Errorf("delete space using Skills API")
		}
		var folder entity.File
		if e = dao.DB.Raw(`WITH RECURSIVE p AS (SELECT * FROM t_ai_files WHERE id=? AND tenant_id=? UNION SELECT f.* FROM t_ai_files f JOIN p ON p.parent_id=f.id AND p.tenant_id=f.tenant_id) SELECT * FROM p WHERE parent_id=? LIMIT 1`, id, tenant, space.FolderID).Scan(&folder).Error; e != nil {
			return true, e
		}
		if folder.ID == "" {
			return true, fmt.Errorf("skill folder not found")
		}
		pending = append(pending, pendingCoreFile{id, folder.ID, folder.Name})
	}
	data, _ := json.Marshal(pending)
	if e = dao.DB.Exec(`UPDATE t_ai_go_skill_spaces SET core_state=jsonb_set(COALESCE(core_state,'{}'::jsonb),'{pending_delete}',?::jsonb) WHERE id=?`, string(data), space.ID).Error; e != nil {
		return true, e
	}
	if e = dao.DB.First(space, "id = ?", space.ID).Error; e != nil {
		return true, e
	}
	return true, resumeCoreFiles(ctx, space)
}
func resumeCoreFiles(ctx context.Context, space *entity.SkillSpace) error {
	doc, _ := skillCoreResources()
	if doc == nil {
		return fmt.Errorf("index unavailable")
	}
	raw, e := json.Marshal(space.CoreState["pending_delete"])
	if e != nil {
		return e
	}
	var pending []pendingCoreFile
	if e = json.Unmarshal(raw, &pending); e != nil {
		return e
	}
	indexer := NewSkillIndexerService()
	for _, item := range pending {
		if e = indexer.DeleteSkillIndex(ctx, space.TenantID, space.ID, item.SkillName, doc); e != nil {
			return e
		}
	}
	for _, item := range pending {
		if e = deleteCoreTree(ctx, space.TenantID, item.ID); e != nil {
			return e
		}
	}
	config, e := dao.NewSkillSearchConfigDAO().GetLatestByTenantID(space.TenantID, space.ID)
	if e != nil && !errors.Is(e, gorm.ErrRecordNotFound) {
		return e
	}
	if e == nil && config.EmbdID != "" {
		// Rebuild the full directory snapshot atomically; stale attachment/version
		// text cannot survive a successful Files deletion.
		if _, e = indexer.ReindexAll(ctx, space.TenantID, space.ID, doc, config.EmbdID); e != nil {
			return e
		}
	}
	return dao.DB.Exec(`UPDATE t_ai_go_skill_spaces SET core_state=NULLIF(core_state-'pending_delete','{}'::jsonb) WHERE id=?`, space.ID).Error
}

func ReadSkillCoreFile(file *entity.File) (bool, []byte, error) {
	space, e := dao.FindSkillCoreSpace(file.ID)
	if e != nil {
		return true, nil, e
	}
	if space == nil {
		return false, nil, nil
	}
	if dao.CoreSpaceUnavailable(space) {
		return true, nil, fmt.Errorf("not found")
	}
	_, blobs := skillCoreResources()
	if blobs == nil || file.Location == nil {
		return true, nil, fmt.Errorf("storage unavailable")
	}
	data, e := blobs.Get(file.ParentID, *file.Location)
	return true, data, e
}

// lockCoreFileMutation closes the deletion-vs-upload window across Go processes.
func lockCoreFileMutation(ctx context.Context, tenant, id string) (func(), error) {
	space, e := dao.FindSkillCoreSpace(id)
	if e != nil {
		return nil, e
	}
	if space == nil {
		return func() {}, nil
	}
	if space.TenantID != tenant {
		return nil, fmt.Errorf("not found")
	}
	db, e := dao.DB.DB()
	if e != nil {
		return nil, e
	}
	conn, e := db.Conn(ctx)
	if e != nil {
		return nil, e
	}
	key := "go-skills:" + tenant + ":" + space.ID
	if _, e = conn.ExecContext(ctx, "SELECT pg_advisory_lock(hashtextextended($1,0))", key); e != nil {
		conn.Close()
		return nil, e
	}
	release := func() {
		_, _ = conn.ExecContext(context.Background(), "SELECT pg_advisory_unlock(hashtextextended($1,0))", key)
		conn.Close()
	}
	fresh, e := dao.NewSkillSpaceDAO().GetByIDAnyStatus(space.ID)
	if e != nil {
		release()
		return nil, e
	}
	if dao.CoreSpaceUnavailable(fresh) {
		release()
		return nil, fmt.Errorf("space unavailable")
	}
	return release, nil
}

// Upload addresses are journaled before Put. Failed writes keep a tombstone:
// a timed-out provider may finish late, so one successful Remove is not final.
type coreUploadAddress struct {
	Bucket string `json:"bucket"`
	Key    string `json:"key"`
}

func beginCoreUpload(spaceID, bucket, key string) error {
	raw, _ := json.Marshal(coreUploadAddress{bucket, key})
	return dao.DB.Exec(`UPDATE t_ai_go_skill_spaces SET core_state=jsonb_set(COALESCE(core_state,'{}'::jsonb),'{pending_upload}',?::jsonb) WHERE id=?`, string(raw), spaceID).Error
}
func claimCoreUpload(spaceID string) error {
	return dao.DB.Exec(`UPDATE t_ai_go_skill_spaces SET core_state=NULLIF(core_state-'pending_upload','{}'::jsonb) WHERE id=?`, spaceID).Error
}
func recoverCoreUploads(ctx context.Context, space *entity.SkillSpace) error {
	_, blobs := skillCoreResources()
	if blobs == nil {
		return fmt.Errorf("storage unavailable")
	}
	var tombstones []coreUploadAddress
	raw, _ := json.Marshal(space.CoreState["upload_tombstones"])
	if string(raw) != "null" {
		if e := json.Unmarshal(raw, &tombstones); e != nil {
			return e
		}
	}
	if pending := space.CoreState["pending_upload"]; pending != nil {
		raw, _ = json.Marshal(pending)
		var address coreUploadAddress
		if e := json.Unmarshal(raw, &address); e != nil {
			return e
		}
		var count int64
		if e := dao.DB.WithContext(ctx).Model(&entity.File{}).Where("tenant_id=? AND parent_id=? AND location=?", space.TenantID, address.Bucket, address.Key).Count(&count).Error; e != nil {
			return e
		}
		if count == 0 {
			tombstones = append(tombstones, address)
		}
		raw, _ = json.Marshal(tombstones)
		if e := dao.DB.WithContext(ctx).Exec(`UPDATE t_ai_go_skill_spaces SET core_state=jsonb_set(COALESCE(core_state,'{}'::jsonb)-'pending_upload','{upload_tombstones}',?::jsonb) WHERE id=?`, string(raw), space.ID).Error; e != nil {
			return e
		}
	}
	for _, address := range tombstones {
		// Never delete an address that was durably claimed by a committed File.
		var count int64
		if e := dao.DB.WithContext(ctx).Model(&entity.File{}).Where("tenant_id=? AND parent_id=? AND location=?", space.TenantID, address.Bucket, address.Key).Count(&count).Error; e != nil {
			return e
		}
		if count == 0 {
			if e := blobs.Remove(address.Bucket, address.Key); e != nil {
				return e
			}
		}
	}
	return nil
}

func mergedCoreError(state entity.JSONMap) entity.JSONMap {
	if state == nil {
		state = entity.JSONMap{}
	}
	state["delete_error"] = "CLEANUP_FAILED"
	return state
}
