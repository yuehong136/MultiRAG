package service

import (
	"context"
	"errors"
	"fmt"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"math"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
)

var errConnectorDenied = errors.New("No authorization.")

// accessibleConnector binds active membership to the resource inside the mutation transaction.
func accessibleConnector(tx *gorm.DB, id, userID string) (*entity.Connector, error) {
	if id == "" || userID == "" {
		return nil, errConnectorDenied
	}
	member := tx.Model(&entity.UserTenant{}).Select("1").Where("user_id = ? AND tenant_id = t_ai_connectors.tenant_id AND status = ? AND role IN ?", userID, "1", []string{"owner", "admin", "normal"})
	var row entity.Connector
	err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ?", id).Where("tenant_id = ? OR EXISTS (?)", userID, member).Take(&row).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, errConnectorDenied
	}
	return &row, err
}

// DeleteConnector atomically cancels pending work and removes links and configuration.
// Documents remain available, matching the connector deletion contract.
func (s *ConnectorService) DeleteConnector(ctx context.Context, id, userID string) (bool, common.ErrorCode, error) {
	err := dao.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		row, err := accessibleConnector(tx, id, userID)
		if err != nil {
			return err
		}
		if err = tx.Model(&entity.SyncLogs{}).Where("connector_id = ? AND status IN ?", id, []string{string(entity.TaskStatusSchedule), string(entity.TaskStatusRunning)}).Update("status", string(entity.TaskStatusCancel)).Error; err != nil {
			return err
		}
		if err = tx.Where("connector_id = ?", id).Delete(&entity.Connector2Kb{}).Error; err != nil {
			return err
		}
		return tx.Delete(row).Error
	})
	return connectorMutationResult(err)
}

func connectorMutationResult(err error) (bool, common.ErrorCode, error) {
	if err == nil {
		return true, common.CodeSuccess, nil
	}
	if errors.Is(err, errConnectorDenied) {
		return false, common.CodeAuthenticationError, errConnectorDenied
	}
	return false, common.CodeServerError, fmt.Errorf("Unable to update connector.")
}

// RebuildConnector checks both resources and the persisted binding before touching work or documents.
func (s *ConnectorService) RebuildConnector(ctx context.Context, id, userID, kbID string) (bool, common.ErrorCode, error) {
	if kbID == "" {
		return false, common.CodeArgumentError, fmt.Errorf("kb_id is required")
	}
	err := dao.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		row, err := accessibleConnector(tx, id, userID)
		if err != nil {
			return err
		}
		var kb entity.Knowledgebase
		if err = tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ? AND tenant_id = ? AND status = ? AND permission IN ?", kbID, row.TenantID, "1", []string{"me", "team"}).Where("tenant_id = ? OR created_by = ? OR permission = ? OR EXISTS (SELECT 1 FROM t_ai_user_tenants m WHERE m.tenant_id = t_ai_knowledgebases.tenant_id AND m.user_id = ? AND m.status = ? AND m.role IN ?)", userID, userID, "team", userID, "1", []string{"owner", "admin"}).Take(&kb).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				return errConnectorDenied
			}
			return err
		}
		var binding entity.Connector2Kb
		if err = tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("connector_id = ? AND kb_id = ?", id, kbID).Take(&binding).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				return errConnectorDenied
			}
			return err
		}
		var docs []*entity.Document
		if err = tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("kb_id = ? AND source_type = ?", kbID, row.Source+"/"+id).Find(&docs).Error; err != nil {
			return err
		}
		if len(docs) > 0 {
			store := s.docEngine
			if store == nil {
				return fmt.Errorf("document engine unavailable")
			}
			ids := make([]string, 0, len(docs))
			for _, doc := range docs {
				ids = append(ids, doc.ID)
			}
			if _, err = store.DeleteChunks(ctx, map[string]interface{}{"doc_id": ids}, "multirag_"+row.TenantID, kbID); err != nil {
				return err
			}
		}
		return s.connectorDAO.RebuildConnector(ctx, tx, row, kbID, docs)
	})
	return connectorMutationResult(err)
}

// ListLog returns deterministic bounded history; zero is not part of the connector log pagination contract.
func (s *ConnectorService) ListLog(ctx context.Context, id, userID string, page, size int) ([]*entity.ConnectorSyncLog, int64, common.ErrorCode, error) {
	if page < 1 || size < 1 || size > 100 || page-1 > math.MaxInt/size {
		return nil, 0, common.CodeArgumentError, fmt.Errorf("invalid log pagination")
	}
	if _, code, err := s.GetConnector(ctx, id, userID); err != nil {
		return nil, 0, code, err
	}
	logs, total, err := s.connectorDAO.ListLogsByConnectorID(ctx, id, userID, (page-1)*size, size)
	if err != nil {
		return nil, 0, common.CodeServerError, fmt.Errorf("Unable to read connector logs.")
	}
	// Error messages can contain source credentials. The count and status remain observable.
	for _, log := range logs {
		if log.ErrorMsg != "" {
			log.ErrorMsg = "Connector synchronization failed."
		}
	}
	return logs, total, common.CodeSuccess, nil
}
