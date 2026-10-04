package dao

import (
	"gorm.io/gorm"
	"multirag/internal/entity"
)

// FindSkillCoreSpace follows tenant-scoped ancestors, including legacy empty
// source_type descendants. Cycles terminate via UNION.
func FindSkillCoreSpace(id string) (*entity.SkillSpace, error) {
	if id == "" || !DB.Migrator().HasTable(&entity.SkillSpace{}) {
		return nil, nil
	}
	var space entity.SkillSpace
	err := DB.Raw(`WITH RECURSIVE parents(id,parent_id,tenant_id) AS (
 SELECT id,parent_id,tenant_id FROM t_ai_files WHERE id = ?
 UNION SELECT f.id,f.parent_id,f.tenant_id FROM t_ai_files f JOIN parents p ON p.parent_id=f.id AND p.tenant_id=f.tenant_id
 ) SELECT s.* FROM t_ai_go_skill_spaces s JOIN parents p ON s.folder_id=p.id AND s.tenant_id=p.tenant_id LIMIT 1`, id).Scan(&space).Error
	if err != nil {
		return nil, err
	}
	if space.ID == "" {
		return nil, nil
	}
	return &space, nil
}
func CoreSpaceUnavailable(s *entity.SkillSpace) bool {
	return s.Status != entity.SpaceStatusActive || s.CoreState["pending_delete"] != nil || s.CoreState["pending_upload"] != nil
}
func excludeUnavailableCore(query *gorm.DB) *gorm.DB {
	if !DB.Migrator().HasTable(&entity.SkillSpace{}) {
		return query
	}
	return query.Where(`id NOT IN (WITH RECURSIVE hidden(id) AS (
 SELECT folder_id FROM t_ai_go_skill_spaces WHERE status <> '1' OR core_state->'pending_delete' IS NOT NULL OR core_state->'pending_upload' IS NOT NULL
 UNION SELECT f.id FROM t_ai_files f JOIN hidden h ON f.parent_id=h.id
 ) SELECT id FROM hidden)`)
}
