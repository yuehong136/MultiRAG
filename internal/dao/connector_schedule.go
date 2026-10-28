package dao

import (
	"context"
	"errors"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/entity"
)

// ScheduleConnectorTasks keeps authorization, binding checks and work creation atomic.
// The caller owns the connector row lock, serializing concurrent schedule requests.
func (dao *ConnectorDAO) ScheduleConnectorTasks(ctx context.Context, tx *gorm.DB, row *entity.Connector, userID string) error {
	var mappings []entity.Connector2Kb
	if err := tx.WithContext(ctx).Clauses(clause.Locking{Strength: "UPDATE"}).Where("connector_id = ?", row.ID).Order("kb_id ASC, id ASC").Find(&mappings).Error; err != nil {
		return err
	}
	seen := map[string]bool{}
	for _, mapping := range mappings {
		if seen[mapping.KbID] {
			continue
		}
		seen[mapping.KbID] = true
		var kb entity.Knowledgebase
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ? AND tenant_id = ? AND status = ? AND permission IN ?", mapping.KbID, row.TenantID, "1", []string{"me", "team"}).
			Where("created_by = ? OR permission = ? OR EXISTS (SELECT 1 FROM t_ai_user_tenants m WHERE m.tenant_id = t_ai_knowledgebases.tenant_id AND m.user_id = ? AND m.status = ? AND m.role IN ('owner','admin'))", userID, "team", userID, "1").Take(&kb).Error; err != nil {
			return err
		}
		tasks := []string{connectorTaskTypeSync}
		if row.Config["sync_deleted_files"] == true {
			tasks = append(tasks, connectorTaskTypePrune)
		}
		for _, task := range tasks {
			if err := scheduleConnectorTask(tx, row.ID, kb.ID, task); err != nil {
				return err
			}
		}
	}
	return nil
}

func scheduleConnectorTask(tx *gorm.DB, connectorID, kbID, taskType string) error {
	var count int64
	if err := tx.Model(&entity.SyncLogs{}).Where("connector_id = ? AND kb_id = ? AND task_type = ? AND status IN ?", connectorID, kbID, taskType, []string{string(entity.TaskStatusSchedule), string(entity.TaskStatusRunning)}).Count(&count).Error; err != nil {
		return err
	}
	if count > 0 {
		return nil
	}
	var start *string
	var total int64
	if taskType == connectorTaskTypeSync {
		var last entity.SyncLogs
		err := tx.Where("connector_id = ? AND kb_id = ? AND task_type = ? AND status = ?", connectorID, kbID, taskType, string(entity.TaskStatusDone)).Order("update_time DESC, id DESC").Take(&last).Error
		if err != nil && !errors.Is(err, gorm.ErrRecordNotFound) {
			return err
		}
		if err == nil {
			start = last.PollRangeEnd
			total = last.TotalDocsIndexed
		}
	}
	now := time.Now().UTC()
	beginning := "0"
	return tx.Create(&entity.SyncLogs{ID: generateUUID(), ConnectorID: connectorID, KbID: kbID, TaskType: taskType, Status: string(entity.TaskStatusSchedule), FromBeginning: &beginning, TimeStarted: &now, PollRangeStart: start, ErrorMsg: "", TotalDocsIndexed: total}).Error
}
