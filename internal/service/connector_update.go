package service

import (
	"context"
	"errors"
	"strings"

	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
)

type UpdateConnectorRequest struct {
	PruneFreq   *int64         `json:"prune_freq"`
	RefreshFreq *int64         `json:"refresh_freq"`
	Config      entity.JSONMap `json:"config"`
	TimeoutSecs *int64         `json:"timeout_secs"`
	Reschedule  *bool          `json:"reschedule"`
	Status      *string        `json:"status"`
}

func (s *ConnectorService) UpdateConnector(ctx context.Context, id, userID string, req UpdateConnectorRequest) (*dao.ConnectorDetail, common.ErrorCode, error) {
	updates := map[string]any{}
	for key, value := range map[string]*int64{"prune_freq": req.PruneFreq, "refresh_freq": req.RefreshFreq, "timeout_secs": req.TimeoutSecs} {
		if value != nil {
			if *value < 0 || key == "timeout_secs" && *value == 0 {
				return nil, common.CodeArgumentError, errors.New("Invalid connector interval or timeout.")
			}
			updates[key] = *value
		}
	}
	if req.Config != nil {
		if value, present := req.Config["sync_deleted_files"]; present {
			if _, ok := value.(bool); !ok {
				return nil, common.CodeArgumentError, errors.New("sync_deleted_files must be a boolean.")
			}
		}
		updates["config"] = req.Config
	}
	status := ""
	if req.Status != nil {
		switch strings.ToUpper(strings.TrimSpace(*req.Status)) {
		case "2", "CANCEL":
			status = string(entity.TaskStatusCancel)
		case "5", "SCHEDULE":
			status = string(entity.TaskStatusSchedule)
		default:
			return nil, common.CodeArgumentError, errors.New("Invalid connector status.")
		}
	}
	reschedule := req.Reschedule != nil && *req.Reschedule
	if reschedule && status == string(entity.TaskStatusCancel) {
		return nil, common.CodeArgumentError, errors.New("Conflicting connector scheduling request.")
	}
	var result *dao.ConnectorDetail
	err := dao.DB.WithContext(ctx).Session(&gorm.Session{Logger: logger.Discard}).Transaction(func(tx *gorm.DB) error {
		row, err := accessibleConnector(tx, id, userID)
		if err != nil {
			return err
		}
		var active int64
		if err := tx.Model(&entity.UserTenant{}).Joins("JOIN t_ai_tenants t ON t.id = t_ai_user_tenants.tenant_id AND t.status = ?", "1").Where("t_ai_user_tenants.tenant_id = ? AND t_ai_user_tenants.user_id = ? AND t_ai_user_tenants.status = ? AND t_ai_user_tenants.role IN ?", row.TenantID, userID, "1", []string{"owner", "admin", "normal"}).Count(&active).Error; err != nil {
			return err
		}
		if active == 0 {
			return errConnectorDenied
		}
		if len(updates) > 0 {
			if err := tx.Model(row).Updates(updates).Error; err != nil {
				return err
			}
		}
		if req.Config != nil {
			row.Config = req.Config
		}
		if reschedule || status == string(entity.TaskStatusCancel) {
			if err := tx.Model(&entity.SyncLogs{}).Where("connector_id = ? AND status IN ?", id, []string{string(entity.TaskStatusSchedule), string(entity.TaskStatusRunning)}).Update("status", string(entity.TaskStatusCancel)).Error; err != nil {
				return err
			}
		}
		if reschedule || status == string(entity.TaskStatusSchedule) {
			if err := s.connectorDAO.ScheduleConnectorTasks(ctx, tx, row, userID); err != nil {
				return err
			}
			status = string(entity.TaskStatusSchedule)
		}
		if status != "" {
			if err := tx.Model(row).Update("status", status).Error; err != nil {
				return err
			}
		}
		// Only public columns are read back into the response.
		result = &dao.ConnectorDetail{}
		return tx.Model(&entity.Connector{}).Select("id", "tenant_id", "name", "source", "input_type", "refresh_freq", "prune_freq", "timeout_secs", "indexing_start", "status").Where("id = ?", id).Take(result).Error
	})
	if err != nil {
		_, code, safe := connectorMutationResult(err)
		return nil, code, safe
	}
	return result, common.CodeSuccess, nil
}
