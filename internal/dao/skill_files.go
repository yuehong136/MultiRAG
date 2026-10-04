package dao

import (
	"errors"
	"gorm.io/gorm"
)

var ErrSkillManagedFile = errors.New("managed skill file is unavailable through Files API")

// SkillManagedFileIDs includes descendants with legacy/empty source_type and uses UNION to stop cycles.
const SkillManagedFileIDs = `WITH RECURSIVE managed(id) AS (
 SELECT id FROM t_ai_files WHERE source_type IN ('skill_space','skill','skill_version','skill_file','python_skill_space_core')
 UNION SELECT f.id FROM t_ai_files f JOIN managed m ON f.parent_id = m.id
) SELECT id FROM managed`

func IsSkillManagedFile(id string) (bool, error) {
	if id == "" {
		return false, nil
	}
	var count int64
	err := DB.Table("t_ai_files").Where("id = ? AND id IN ("+SkillManagedFileIDs+")", id).Count(&count).Error
	return count > 0, err
}
func GuardSkillFile(id string) error {
	managed, err := IsSkillManagedFile(id)
	if err != nil {
		return err
	}
	if managed {
		return ErrSkillManagedFile
	}
	space, err := FindSkillCoreSpace(id)
	if err != nil {
		return err
	}
	if space != nil && CoreSpaceUnavailable(space) {
		return ErrSkillManagedFile
	}
	return nil
}
func GuardSkillFileTree(id string) error {
	if err := GuardSkillFile(id); err != nil {
		return err
	}
	var count int64
	err := DB.Raw(`WITH RECURSIVE descendants(id) AS (SELECT id FROM t_ai_files WHERE id = ? UNION SELECT f.id FROM t_ai_files f JOIN descendants d ON f.parent_id = d.id) SELECT count(*) FROM t_ai_files WHERE id IN (SELECT id FROM descendants) AND source_type IN ('skill_space','skill','skill_version','skill_file','python_skill_space_core')`, id).Scan(&count).Error
	if err != nil {
		return err
	}
	if count > 0 {
		return ErrSkillManagedFile
	}
	return nil
}
func ExcludeSkillFiles(query *gorm.DB) *gorm.DB {
	return excludeUnavailableCore(query.Where("id NOT IN (" + SkillManagedFileIDs + ")"))
}
