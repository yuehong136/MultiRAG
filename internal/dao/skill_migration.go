package dao

import (
	"fmt"
	"gorm.io/gorm"
	"multirag/internal/entity"
)

// MigrateSkillCore owns only Go core tables; it never migrates or deletes the
// legacy asset schema or another backend's data. PostgreSQL is the supported DB.
func MigrateSkillCore(db *gorm.DB) error {
	if db.Dialector.Name() != "postgres" {
		return fmt.Errorf("Go Skills core requires PostgreSQL")
	}
	for _, table := range []string{"t_ai_python_skill_spaces", "t_ai_skill_spaces"} {
		if db.Migrator().HasTable(table) {
			var count int64
			q := db.Table(table)
			if table == "t_ai_skill_spaces" {
				q = q.Where("backend_owner = 'python' AND state <> 'deleted'")
			}
			if err := q.Count(&count).Error; err != nil {
				return err
			}
			if count > 0 {
				return fmt.Errorf("Go Skills core requires an independent database; Python assets exist")
			}
		}
	}
	return db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Exec("SELECT pg_advisory_xact_lock(hashtextextended('go-skill-core-schema-v1',0))").Error; err != nil {
			return err
		}
		if err := tx.AutoMigrate(&entity.SkillSpace{}, &entity.SkillSearchConfig{}); err != nil {
			return err
		}
		for _, sql := range []string{
			"CREATE UNIQUE INDEX IF NOT EXISTS go_skill_space_name ON t_ai_go_skill_spaces(tenant_id,name) WHERE status <> '0'",
			"CREATE UNIQUE INDEX IF NOT EXISTS go_skill_config_space ON t_ai_go_skill_search_configs(tenant_id,space_id) WHERE status = '1'",
		} {
			if err := tx.Exec(sql).Error; err != nil {
				return err
			}
		}
		return nil
	})
}
