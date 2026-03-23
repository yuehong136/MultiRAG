//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

package dao

import (
	"fmt"
	"multirag/internal/logger"
	"strings"

	"go.uber.org/zap"
	"gorm.io/gorm"
)

// RunMigrations runs all manual database migrations
// These are migrations that cannot be handled by AutoMigrate alone
func RunMigrations(db *gorm.DB) error {
	// Check if t_ai_tenant_llms table has composite primary key and migrate to ID primary key
	if err := migrateTenantLLMPrimaryKey(db); err != nil {
		return fmt.Errorf("failed to migrate t_ai_tenant_llms primary key: %w", err)
	}

	// Rename columns (correct typos)
	if err := renameColumnIfExists(db, "t_ai_tasks", "process_duation", "process_duration"); err != nil {
		return fmt.Errorf("failed to rename t_ai_tasks.process_duation: %w", err)
	}
	if err := renameColumnIfExists(db, "t_ai_documents", "process_duation", "process_duration"); err != nil {
		return fmt.Errorf("failed to rename t_ai_documents.process_duation: %w", err)
	}

	// Add unique index on t_ai_users.email
	if err := migrateAddUniqueEmail(db); err != nil {
		return fmt.Errorf("failed to add unique index on t_ai_users.email: %w", err)
	}

	// Modify column types that AutoMigrate may not handle correctly
	if err := modifyColumnTypes(db); err != nil {
		return fmt.Errorf("failed to modify column types: %w", err)
	}

	logger.Info("All manual migrations completed successfully")
	return nil
}

// migrateTenantLLMPrimaryKey migrates t_ai_tenant_llms from composite primary key to ID primary key
// This corresponds to Python's update_tenant_llm_to_id_primary_key function
func migrateTenantLLMPrimaryKey(db *gorm.DB) error {
	// Check if t_ai_tenant_llms table exists
	if !db.Migrator().HasTable("t_ai_tenant_llms") {
		return nil
	}

	// Check if 'id' column already exists using raw SQL
	var idColumnExists int64
	if IsPostgres() {
		err := db.Raw(`
			SELECT COUNT(*) FROM information_schema.columns
			WHERE table_schema = current_schema() AND table_name = 't_ai_tenant_llms' AND column_name = 'id'
		`).Scan(&idColumnExists).Error
		if err != nil {
			return err
		}
	} else {
		err := db.Raw(`
			SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
			WHERE TABLE_NAME = 't_ai_tenant_llms' AND COLUMN_NAME = 'id'
		`).Scan(&idColumnExists).Error
		if err != nil {
			return err
		}
	}

	if idColumnExists > 0 {
		// Check if id is already a primary key with auto_increment
		var count int64
		if IsPostgres() {
			err := db.Raw(`
				SELECT COUNT(*) FROM information_schema.columns
				WHERE table_schema = current_schema()
				AND table_name = 't_ai_tenant_llms'
				AND column_name = 'id'
				AND column_default LIKE 'nextval%'
			`).Scan(&count).Error
			if err != nil {
				return err
			}
		} else {
			err := db.Raw(`
				SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
				WHERE TABLE_NAME = 't_ai_tenant_llms'
				AND COLUMN_NAME = 'id'
				AND EXTRA LIKE '%auto_increment%'
			`).Scan(&count).Error
			if err != nil {
				return err
			}
		}
		if count > 0 {
			// Already migrated
			return nil
		}
	}

	logger.Info("Migrating t_ai_tenant_llms to use ID primary key...")

	// Start transaction
	return db.Transaction(func(tx *gorm.DB) error {
		// Check for temp_id column and drop it if exists
		var tempIdExists int64
		if IsPostgres() {
			tx.Raw(`SELECT COUNT(*) FROM information_schema.columns
				WHERE table_schema = current_schema() AND table_name = 't_ai_tenant_llms' AND column_name = 'temp_id'`).Scan(&tempIdExists)
		} else {
			tx.Raw(`SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
				WHERE TABLE_NAME = 't_ai_tenant_llms' AND COLUMN_NAME = 'temp_id'`).Scan(&tempIdExists)
		}
		if tempIdExists > 0 {
			if err := tx.Exec("ALTER TABLE t_ai_tenant_llms DROP COLUMN temp_id").Error; err != nil {
				logger.Warn("Failed to drop temp_id column", zap.Error(err))
			}
		}

		// Check if there's already an 'id' column
		if IsPostgres() {
			// Drop existing composite primary key constraint first
			if err := tx.Exec(`
				ALTER TABLE t_ai_tenant_llms DROP CONSTRAINT IF EXISTS t_ai_tenant_llms_pkey
			`).Error; err != nil {
				logger.Warn("Failed to drop existing primary key constraint", zap.Error(err))
			}

			if idColumnExists > 0 {
				// Check id column data type: if varchar, must drop and recreate
				var idDataType string
				tx.Raw(`
					SELECT data_type FROM information_schema.columns
					WHERE table_schema = current_schema() AND table_name = 't_ai_tenant_llms' AND column_name = 'id'
				`).Scan(&idDataType)

				if idDataType == "bigint" || idDataType == "integer" {
					// Integer id exists, just add identity and primary key
					if err := tx.Exec(`
						ALTER TABLE t_ai_tenant_llms
						ALTER COLUMN id SET NOT NULL,
						ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY,
						ADD PRIMARY KEY (id)
					`).Error; err != nil {
						return fmt.Errorf("failed to modify id column to identity: %w", err)
					}
				} else {
					// id is varchar or other type, drop and recreate as BIGSERIAL
					if err := tx.Exec(`ALTER TABLE t_ai_tenant_llms DROP COLUMN id`).Error; err != nil {
						return fmt.Errorf("failed to drop varchar id column: %w", err)
					}
					if err := tx.Exec(`ALTER TABLE t_ai_tenant_llms ADD COLUMN id BIGSERIAL PRIMARY KEY`).Error; err != nil {
						return fmt.Errorf("failed to add id column as BIGSERIAL: %w", err)
					}
				}
			} else {
				// Add id column as BIGSERIAL primary key
				if err := tx.Exec(`
					ALTER TABLE t_ai_tenant_llms ADD COLUMN id BIGSERIAL PRIMARY KEY
				`).Error; err != nil {
					return fmt.Errorf("failed to add id column: %w", err)
				}
			}
		} else {
			if idColumnExists > 0 {
				// Modify existing id column to be auto_increment primary key
				if err := tx.Exec(`
					ALTER TABLE t_ai_tenant_llms
					MODIFY COLUMN id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY
				`).Error; err != nil {
					return fmt.Errorf("failed to modify id column: %w", err)
				}
			} else {
				// Add id column as auto_increment primary key
				if err := tx.Exec(`
					ALTER TABLE t_ai_tenant_llms
					ADD COLUMN id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY FIRST
				`).Error; err != nil {
					return fmt.Errorf("failed to add id column: %w", err)
				}
			}
		}

		// Add unique index on (tenant_id, llm_factory, llm_name)
		var idxExists int64
		if IsPostgres() {
			tx.Raw(`SELECT COUNT(*) FROM pg_indexes
				WHERE schemaname = current_schema() AND tablename = 't_ai_tenant_llms' AND indexname = 'idx_tenant_llm_unique'`).Scan(&idxExists)
		} else {
			tx.Raw(`SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
				WHERE TABLE_NAME = 't_ai_tenant_llms' AND INDEX_NAME = 'idx_tenant_llm_unique'`).Scan(&idxExists)
		}
		if idxExists == 0 {
			if IsPostgres() {
				if err := tx.Exec(`
					CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_llm_unique
					ON t_ai_tenant_llms (tenant_id, llm_factory, llm_name)
				`).Error; err != nil {
					logger.Warn("Failed to add unique index idx_tenant_llm_unique", zap.Error(err))
				}
			} else {
				if err := tx.Exec(`
					ALTER TABLE t_ai_tenant_llms
					ADD UNIQUE INDEX idx_tenant_llm_unique (tenant_id, llm_factory, llm_name)
				`).Error; err != nil {
					logger.Warn("Failed to add unique index idx_tenant_llm_unique", zap.Error(err))
				}
			}
		}

		logger.Info("t_ai_tenant_llms primary key migration completed")
		return nil
	})
}

// migrateAddUniqueEmail adds unique index on t_ai_users.email
func migrateAddUniqueEmail(db *gorm.DB) error {
	if !db.Migrator().HasTable("t_ai_users") {
		return nil
	}

	// Check if unique index already exists using raw SQL
	var count int64
	if IsPostgres() {
		db.Raw(`SELECT COUNT(*) FROM pg_indexes
			WHERE schemaname = current_schema() AND tablename = 't_ai_users' AND indexname = 'idx_user_email_unique'`).Scan(&count)
	} else {
		db.Raw(`SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
			WHERE TABLE_NAME = 't_ai_users' AND INDEX_NAME = 'idx_user_email_unique'`).Scan(&count)
	}
	if count > 0 {
		return nil
	}

	// Check if there's a duplicate email issue first
	var duplicateCount int64
	if IsPostgres() {
		err := db.Raw(`
			SELECT COUNT(*) FROM (
				SELECT email FROM t_ai_users GROUP BY email HAVING COUNT(*) > 1
			) AS duplicates
		`).Scan(&duplicateCount).Error
		if err != nil {
			return err
		}
	} else {
		err := db.Raw(`
			SELECT COUNT(*) FROM (
				SELECT email FROM t_ai_users GROUP BY email HAVING COUNT(*) > 1
			) AS duplicates
		`).Scan(&duplicateCount).Error
		if err != nil {
			return err
		}
	}

	if duplicateCount > 0 {
		logger.Warn("Found duplicate emails in t_ai_users table, cannot add unique index", zap.Int64("count", duplicateCount))
		return nil
	}

	logger.Info("Adding unique index on t_ai_users.email...")
	if IsPostgres() {
		if err := db.Exec(`CREATE UNIQUE INDEX IF NOT EXISTS idx_user_email_unique ON t_ai_users (email)`).Error; err != nil {
			errStr := err.Error()
			if strings.Contains(errStr, "already exists") {
				logger.Info("Index already exists, skipping", zap.String("error", errStr))
				return nil
			}
			return fmt.Errorf("failed to add unique index on email: %w", err)
		}
	} else {
		if err := db.Exec(`ALTER TABLE t_ai_users ADD UNIQUE INDEX idx_user_email_unique (email)`).Error; err != nil {
			// Check if error is MySQL duplicate index error (Error 1061)
			errStr := err.Error()
			if strings.Contains(errStr, "Error 1061") && strings.Contains(errStr, "Duplicate key name") {
				logger.Info("Index already exists, skipping", zap.String("error", errStr))
				return nil
			}
			return fmt.Errorf("failed to add unique index on email: %w", err)
		}
	}

	return nil
}

// modifyColumnTypes modifies column types that need explicit ALTER statements
func modifyColumnTypes(db *gorm.DB) error {
	// Helper function to check if column exists
	columnExists := func(table, column string) bool {
		var count int64
		if IsPostgres() {
			db.Raw(`SELECT COUNT(*) FROM information_schema.columns
				WHERE table_schema = current_schema() AND table_name = ? AND column_name = ?`, table, column).Scan(&count)
		} else {
			db.Raw(`SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
				WHERE TABLE_NAME = ? AND COLUMN_NAME = ?`, table, column).Scan(&count)
		}
		return count > 0
	}

	// t_ai_dialogs.top_k: ensure it's INTEGER with default 1024
	if db.Migrator().HasTable("t_ai_dialogs") && columnExists("t_ai_dialogs", "top_k") {
		if IsPostgres() {
			if err := db.Exec(`ALTER TABLE t_ai_dialogs ALTER COLUMN top_k TYPE BIGINT`).Error; err != nil {
				logger.Warn("Failed to modify t_ai_dialogs.top_k type", zap.Error(err))
			}
			if err := db.Exec(`ALTER TABLE t_ai_dialogs ALTER COLUMN top_k SET DEFAULT 1024`).Error; err != nil {
				logger.Warn("Failed to set t_ai_dialogs.top_k default", zap.Error(err))
			}
			if err := db.Exec(`ALTER TABLE t_ai_dialogs ALTER COLUMN top_k SET NOT NULL`).Error; err != nil {
				logger.Warn("Failed to set t_ai_dialogs.top_k not null", zap.Error(err))
			}
		} else {
			if err := db.Exec(`ALTER TABLE t_ai_dialogs MODIFY COLUMN top_k BIGINT NOT NULL DEFAULT 1024`).Error; err != nil {
				logger.Warn("Failed to modify t_ai_dialogs.top_k", zap.Error(err))
			}
		}
	}

	// t_ai_tenant_llms.api_key: ensure it's TEXT type
	if db.Migrator().HasTable("t_ai_tenant_llms") && columnExists("t_ai_tenant_llms", "api_key") {
		if IsPostgres() {
			if err := db.Exec(`ALTER TABLE t_ai_tenant_llms ALTER COLUMN api_key TYPE TEXT`).Error; err != nil {
				logger.Warn("Failed to modify t_ai_tenant_llms.api_key", zap.Error(err))
			}
		} else {
			if err := db.Exec(`ALTER TABLE t_ai_tenant_llms MODIFY COLUMN api_key LONGTEXT`).Error; err != nil {
				logger.Warn("Failed to modify t_ai_tenant_llms.api_key", zap.Error(err))
			}
		}
	}

	// t_ai_api_tokens.dialog_id: ensure it's varchar(32)
	if db.Migrator().HasTable("t_ai_api_tokens") && columnExists("t_ai_api_tokens", "dialog_id") {
		if IsPostgres() {
			if err := db.Exec(`ALTER TABLE t_ai_api_tokens ALTER COLUMN dialog_id TYPE VARCHAR(32)`).Error; err != nil {
				logger.Warn("Failed to modify t_ai_api_tokens.dialog_id", zap.Error(err))
			}
		} else {
			if err := db.Exec(`ALTER TABLE t_ai_api_tokens MODIFY COLUMN dialog_id VARCHAR(32)`).Error; err != nil {
				logger.Warn("Failed to modify t_ai_api_tokens.dialog_id", zap.Error(err))
			}
		}
	}

	// t_ai_canvas_templates.title and description: ensure they're TEXT type (same as Python JSONField)
	// Note: Python's JSONField uses null=True with application-level default, not database DEFAULT
	if db.Migrator().HasTable("t_ai_canvas_templates") {
		if columnExists("t_ai_canvas_templates", "title") {
			if IsPostgres() {
				if err := db.Exec(`ALTER TABLE t_ai_canvas_templates ALTER COLUMN title TYPE TEXT`).Error; err != nil {
					logger.Warn("Failed to modify t_ai_canvas_templates.title", zap.Error(err))
				}
			} else {
				if err := db.Exec(`ALTER TABLE t_ai_canvas_templates MODIFY COLUMN title LONGTEXT NULL`).Error; err != nil {
					logger.Warn("Failed to modify t_ai_canvas_templates.title", zap.Error(err))
				}
			}
		}
		if columnExists("t_ai_canvas_templates", "description") {
			if IsPostgres() {
				if err := db.Exec(`ALTER TABLE t_ai_canvas_templates ALTER COLUMN description TYPE TEXT`).Error; err != nil {
					logger.Warn("Failed to modify t_ai_canvas_templates.description", zap.Error(err))
				}
			} else {
				if err := db.Exec(`ALTER TABLE t_ai_canvas_templates MODIFY COLUMN description LONGTEXT NULL`).Error; err != nil {
					logger.Warn("Failed to modify t_ai_canvas_templates.description", zap.Error(err))
				}
			}
		}
	}

	// t_ai_system_settings.value: ensure it's TEXT
	if db.Migrator().HasTable("t_ai_system_settings") && columnExists("t_ai_system_settings", "value") {
		if IsPostgres() {
			if err := db.Exec(`ALTER TABLE t_ai_system_settings ALTER COLUMN value TYPE TEXT`).Error; err != nil {
				logger.Warn("Failed to modify t_ai_system_settings.value", zap.Error(err))
			}
			if err := db.Exec(`ALTER TABLE t_ai_system_settings ALTER COLUMN value SET NOT NULL`).Error; err != nil {
				logger.Warn("Failed to set t_ai_system_settings.value not null", zap.Error(err))
			}
		} else {
			if err := db.Exec(`ALTER TABLE t_ai_system_settings MODIFY COLUMN value LONGTEXT NOT NULL`).Error; err != nil {
				logger.Warn("Failed to modify t_ai_system_settings.value", zap.Error(err))
			}
		}
	}

	// t_ai_knowledgebases.raptor_task_finish_at: ensure it's DateTime/Timestamp
	if db.Migrator().HasTable("t_ai_knowledgebases") && columnExists("t_ai_knowledgebases", "raptor_task_finish_at") {
		if IsPostgres() {
			if err := db.Exec(`ALTER TABLE t_ai_knowledgebases ALTER COLUMN raptor_task_finish_at TYPE TIMESTAMP`).Error; err != nil {
				logger.Warn("Failed to modify t_ai_knowledgebases.raptor_task_finish_at", zap.Error(err))
			}
		} else {
			if err := db.Exec(`ALTER TABLE t_ai_knowledgebases MODIFY COLUMN raptor_task_finish_at DATETIME`).Error; err != nil {
				logger.Warn("Failed to modify t_ai_knowledgebases.raptor_task_finish_at", zap.Error(err))
			}
		}
	}

	// t_ai_knowledgebases.mindmap_task_finish_at: ensure it's DateTime/Timestamp
	if db.Migrator().HasTable("t_ai_knowledgebases") && columnExists("t_ai_knowledgebases", "mindmap_task_finish_at") {
		if IsPostgres() {
			if err := db.Exec(`ALTER TABLE t_ai_knowledgebases ALTER COLUMN mindmap_task_finish_at TYPE TIMESTAMP`).Error; err != nil {
				logger.Warn("Failed to modify t_ai_knowledgebases.mindmap_task_finish_at", zap.Error(err))
			}
		} else {
			if err := db.Exec(`ALTER TABLE t_ai_knowledgebases MODIFY COLUMN mindmap_task_finish_at DATETIME`).Error; err != nil {
				logger.Warn("Failed to modify t_ai_knowledgebases.mindmap_task_finish_at", zap.Error(err))
			}
		}
	}

	return nil
}

// renameColumnIfExists renames a column if it exists and the new column doesn't exist
func renameColumnIfExists(db *gorm.DB, tableName, oldName, newName string) error {
	if !db.Migrator().HasTable(tableName) {
		return nil
	}

	// Helper to check if column exists
	columnExists := func(column string) bool {
		var count int64
		if IsPostgres() {
			db.Raw(`SELECT COUNT(*) FROM information_schema.columns
				WHERE table_schema = current_schema() AND table_name = ? AND column_name = ?`, tableName, column).Scan(&count)
		} else {
			db.Raw(`SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
				WHERE TABLE_NAME = ? AND COLUMN_NAME = ?`, tableName, column).Scan(&count)
		}
		return count > 0
	}

	// Check if old column exists
	if !columnExists(oldName) {
		return nil
	}

	// Check if new column already exists
	if columnExists(newName) {
		// Both exist, drop the old one
		logger.Warn("Both old and new columns exist, dropping old one",
			zap.String("table", tableName),
			zap.String("oldColumn", oldName),
			zap.String("newColumn", newName))
		return db.Migrator().DropColumn(tableName, oldName)
	}

	logger.Info("Renaming column",
		zap.String("table", tableName),
		zap.String("oldColumn", oldName),
		zap.String("newColumn", newName))
	return db.Migrator().RenameColumn(tableName, oldName, newName)
}

// addColumnIfNotExists adds a column if it doesn't exist
func addColumnIfNotExists(db *gorm.DB, tableName, columnName, columnDef string) error {
	if !db.Migrator().HasTable(tableName) {
		return nil
	}

	// Check if column exists using raw SQL
	var count int64
	if IsPostgres() {
		db.Raw(`SELECT COUNT(*) FROM information_schema.columns
			WHERE table_schema = current_schema() AND table_name = ? AND column_name = ?`, tableName, columnName).Scan(&count)
	} else {
		db.Raw(`SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
			WHERE TABLE_NAME = ? AND COLUMN_NAME = ?`, tableName, columnName).Scan(&count)
	}
	if count > 0 {
		return nil
	}

	logger.Info("Adding column",
		zap.String("table", tableName),
		zap.String("column", columnName))
	sql := fmt.Sprintf("ALTER TABLE %s ADD COLUMN %s %s", tableName, columnName, columnDef)
	return db.Exec(sql).Error
}
