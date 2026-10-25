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
	"context"
	"gorm.io/gorm"
	"multirag/internal/entity"
	"time"
)

// ConnectorDAO connector data access object
type ConnectorDAO struct{}

// NewConnectorDAO create connector DAO
func NewConnectorDAO() *ConnectorDAO {
	return &ConnectorDAO{}
}

// ConnectorListItem connector list item (subset of fields)
type ConnectorListItem struct {
	ID     string `json:"id"`
	Name   string `json:"name"`
	Source string `json:"source"`
	Status string `json:"status"`
}

// ListByTenantID list connectors by tenant ID
// Only selects id, name, source, status fields (matching Python implementation)
func (dao *ConnectorDAO) ListByTenantID(tenantID string) ([]*ConnectorListItem, error) {
	var connectors []*ConnectorListItem

	err := DB.Model(&entity.Connector{}).
		Select("id", "name", "source", "status").
		Where("tenant_id = ?", tenantID).
		Find(&connectors).Error

	if err != nil {
		return nil, err
	}

	return connectors, nil
}

// GetByID get connector by ID
func (dao *ConnectorDAO) GetByID(id string) (*entity.Connector, error) {
	var connector entity.Connector
	err := DB.Where("id = ?", id).First(&connector).Error
	if err != nil {
		return nil, err
	}
	return &connector, nil
}

// Create create a new connector
func (dao *ConnectorDAO) Create(connector *entity.Connector) error {
	return DB.Create(connector).Error
}

// UpdateByID update connector by ID
func (dao *ConnectorDAO) UpdateByID(id string, updates map[string]interface{}) error {
	return DB.Model(&entity.Connector{}).Where("id = ?", id).Updates(updates).Error
}

// DeleteByID delete connector by ID
func (dao *ConnectorDAO) DeleteByID(id string) error {
	return DB.Where("id = ?", id).Delete(&entity.Connector{}).Error
}

// ConnectorDatasetListItem is the public metadata for a linked connector.
// Credentials and connector config are intentionally excluded.
type ConnectorDatasetListItem struct {
	ID        string `json:"id"`
	Source    string `json:"source"`
	Name      string `json:"name"`
	AutoParse string `json:"auto_parse"`
	Status    string `json:"status"`
}

// ListByDatasetID reads links only when the connector belongs to the dataset tenant.
func (dao *ConnectorDAO) ListByDatasetID(ctx context.Context, datasetID, tenantID string) ([]*ConnectorDatasetListItem, error) {
	connectors := make([]*ConnectorDatasetListItem, 0)
	err := DB.WithContext(ctx).Model(&entity.Connector2Kb{}).
		Select("connector.id, connector.source, connector.name, t_ai_connector2kb.auto_parse, connector.status").
		Joins("JOIN t_ai_connectors AS connector ON t_ai_connector2kb.connector_id = connector.id").
		Where("t_ai_connector2kb.kb_id = ? AND connector.tenant_id = ?", datasetID, tenantID).
		Order("connector.id").
		Scan(&connectors).Error
	return connectors, err
}

// ConnectorDetail is public connector metadata. Config and credentials are never selected.
type ConnectorDetail struct {
	ID            string     `json:"id"`
	TenantID      string     `json:"tenant_id"`
	Name          string     `json:"name"`
	Source        string     `json:"source"`
	InputType     string     `json:"input_type"`
	RefreshFreq   int64      `json:"refresh_freq"`
	PruneFreq     int64      `json:"prune_freq"`
	TimeoutSecs   int64      `json:"timeout_secs"`
	IndexingStart *time.Time `json:"indexing_start,omitempty"`
	Status        string     `json:"status"`
}

// GetAccessibleDetail applies the current connector resource access policy in SQL.
// Unknown IDs and inaccessible tenants both produce gorm.ErrRecordNotFound.
func (dao *ConnectorDAO) GetAccessibleDetail(ctx context.Context, connectorID, userID string) (*ConnectorDetail, error) {
	var detail ConnectorDetail
	membership := DB.WithContext(ctx).Model(&entity.UserTenant{}).
		Select("1").Where("user_id = ? AND tenant_id = t_ai_connectors.tenant_id AND status = ? AND role IN ?", userID, "1", []string{"owner", "admin", "normal"})
	err := DB.WithContext(ctx).Model(&entity.Connector{}).
		Select("id", "tenant_id", "name", "source", "input_type", "refresh_freq", "prune_freq", "timeout_secs", "indexing_start", "status").
		Where("id = ?", connectorID).
		Where("tenant_id = ? OR EXISTS (?)", userID, membership).
		Take(&detail).Error
	if err != nil {
		return nil, err
	}
	return &detail, nil
}

// RebuildConnector replaces old connector documents with scheduled sync tasks.
func (dao *ConnectorDAO) RebuildConnector(ctx context.Context, db *gorm.DB, connector *entity.Connector, kbID string, documents []*entity.Document) error {
	return db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if err := tx.Where("connector_id = ? AND kb_id = ?", connector.ID, kbID).Delete(&entity.SyncLogs{}).Error; err != nil {
			return err
		}

		if len(documents) > 0 {
			docIDs := make([]string, 0, len(documents))
			var tokenNum int64
			var chunkNum int64
			for _, document := range documents {
				docIDs = append(docIDs, document.ID)
				tokenNum += document.TokenNum
				chunkNum += document.ChunkNum
			}

			var mappings []entity.File2Document
			if err := tx.Where("document_id IN ?", docIDs).Find(&mappings).Error; err != nil {
				return err
			}
			fileIDs := make([]string, 0, len(mappings))
			seenFileIDs := make(map[string]struct{}, len(mappings))
			for _, mapping := range mappings {
				if mapping.FileID == nil || *mapping.FileID == "" {
					continue
				}
				if _, ok := seenFileIDs[*mapping.FileID]; ok {
					continue
				}
				seenFileIDs[*mapping.FileID] = struct{}{}
				fileIDs = append(fileIDs, *mapping.FileID)
			}

			if err := tx.Where("doc_id IN ?", docIDs).Delete(&entity.Task{}).Error; err != nil {
				return err
			}
			if err := tx.Where("document_id IN ?", docIDs).Delete(&entity.File2Document{}).Error; err != nil {
				return err
			}
			if len(fileIDs) > 0 {
				if err := tx.Unscoped().
					Where("id IN ? AND tenant_id = ? AND source_type = ? AND NOT EXISTS (SELECT 1 FROM t_ai_file2documents AS other WHERE other.file_id = t_ai_files.id)", fileIDs, connector.TenantID, string(entity.FileSourceKnowledgebase)).
					Delete(&entity.File{}).Error; err != nil {
					return err
				}
			}
			if err := tx.Where("id IN ?", docIDs).Delete(&entity.Document{}).Error; err != nil {
				return err
			}
			if err := tx.Model(&entity.Knowledgebase{}).
				Where("id = ?", kbID).
				Updates(map[string]interface{}{
					"doc_num":   gorm.Expr("doc_num - ?", len(docIDs)),
					"token_num": gorm.Expr("token_num - ?", tokenNum),
					"chunk_num": gorm.Expr("chunk_num - ?", chunkNum),
				}).Error; err != nil {
				return err
			}
		}

		if err := tx.Model(&entity.Connector{}).
			Where("id = ?", connector.ID).
			Update("status", string(entity.TaskStatusSchedule)).Error; err != nil {
			return err
		}

		if err := createRebuildSyncLog(tx, connector.ID, kbID, connectorTaskTypeSync, true); err != nil {
			return err
		}
		if syncDeletedFiles, _ := connector.Config["sync_deleted_files"].(bool); syncDeletedFiles {
			if err := createRebuildSyncLog(tx, connector.ID, kbID, connectorTaskTypePrune, false); err != nil {
				return err
			}
		}
		return nil
	})
}

const (
	connectorTaskTypeSync  = "sync"
	connectorTaskTypePrune = "prune"
)

func createRebuildSyncLog(tx *gorm.DB, connectorID, kbID, taskType string, reindex bool) error {
	fromBeginning := "0"
	if reindex {
		fromBeginning = "1"
	}
	now := time.Now().UTC()
	return tx.Create(&entity.SyncLogs{
		ID:               generateUUID(),
		ConnectorID:      connectorID,
		KbID:             kbID,
		TaskType:         taskType,
		Status:           string(entity.TaskStatusSchedule),
		FromBeginning:    &fromBeginning,
		TimeStarted:      &now,
		ErrorMsg:         "",
		TotalDocsIndexed: 0,
	}).Error
}

// ListLogsByConnectorID lists sync logs for one connector with pagination.
func (dao *ConnectorDAO) ListLogsByConnectorID(ctx context.Context, connectorID, userID string, offset, limit int) ([]*entity.ConnectorSyncLog, int64, error) {
	baseQuery := DB.WithContext(ctx).Model(&entity.SyncLogs{}).
		Joins("JOIN t_ai_connectors AS connector ON t_ai_sync_logs.connector_id = connector.id").
		Joins("JOIN t_ai_connector2kb ON t_ai_sync_logs.connector_id = t_ai_connector2kb.connector_id AND t_ai_sync_logs.kb_id = t_ai_connector2kb.kb_id").
		Joins("JOIN t_ai_knowledgebases ON t_ai_sync_logs.kb_id = t_ai_knowledgebases.id AND t_ai_knowledgebases.tenant_id = connector.tenant_id").
		Where("t_ai_sync_logs.connector_id = ?", connectorID).
		Where("t_ai_knowledgebases.status = ? AND t_ai_knowledgebases.permission IN ?", "1", []string{"me", "team"}).
		Where("connector.tenant_id = ? OR t_ai_knowledgebases.created_by = ? OR t_ai_knowledgebases.permission = ? OR EXISTS (SELECT 1 FROM t_ai_user_tenants m WHERE m.tenant_id = connector.tenant_id AND m.user_id = ? AND m.status = ? AND m.role IN ?)", userID, userID, "team", userID, "1", []string{"owner", "admin"})

	var total int64
	if err := baseQuery.Distinct("t_ai_sync_logs.id").Count(&total).Error; err != nil {
		return nil, 0, err
	}

	logs := make([]*entity.ConnectorSyncLog, 0)
	err := baseQuery.
		Select(
			"t_ai_sync_logs.id",
			"t_ai_sync_logs.connector_id",
			"t_ai_sync_logs.task_type",
			"t_ai_sync_logs.kb_id",
			"t_ai_sync_logs.update_date",
			"t_ai_sync_logs.update_time",
			"t_ai_sync_logs.new_docs_indexed",
			"t_ai_sync_logs.total_docs_indexed",
			"t_ai_sync_logs.docs_removed_from_index",
			"t_ai_sync_logs.error_msg",
			"t_ai_sync_logs.error_count",
			"t_ai_sync_logs.time_started",
			"connector.refresh_freq AS refresh_freq",
			"connector.prune_freq AS prune_freq",
			"t_ai_knowledgebases.name AS kb_name",
			"t_ai_sync_logs.status",
		).
		Distinct().
		Order("t_ai_sync_logs.update_time DESC, t_ai_sync_logs.id DESC").
		Offset(offset).
		Limit(limit).
		Scan(&logs).Error
	if err != nil {
		return nil, 0, err
	}

	return logs, total, nil
}

// ListAccessible returns owned and actively joined tenants under the detail policy.
func (dao *ConnectorDAO) ListAccessible(userID string) ([]*ConnectorListItem, error) {
	result := make([]*ConnectorListItem, 0)
	membership := DB.Model(&entity.UserTenant{}).Select("1").Where("user_id = ? AND tenant_id = t_ai_connectors.tenant_id AND status = ? AND role IN ?", userID, "1", []string{"owner", "admin", "normal"})
	err := DB.Model(&entity.Connector{}).Select("id", "name", "source", "status").Where("tenant_id = ? OR EXISTS (?)", userID, membership).Order("id").Scan(&result).Error
	return result, err
}
