package service

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
)

type DocumentMetadataConfigRequest struct {
	Metadata json.RawMessage `json:"metadata"`
}

// UpdateDocumentMetadataConfig replaces the full template while preserving the
// latest other parser fields under the same document lock as parser updates.
func (s *DatasetsService) UpdateDocumentMetadataConfig(ctx context.Context, userID, datasetID, documentID string, req DocumentMetadataConfigRequest) (map[string]any, common.ErrorCode, error) {
	if userID == "" {
		return nil, common.CodeAuthenticationError, errors.New("No authorization.")
	}
	id, err := normalizeDatasetID(strings.TrimSpace(datasetID))
	if err != nil || strings.TrimSpace(documentID) == "" {
		return nil, common.CodeArgumentError, errors.New("Invalid dataset or document ID.")
	}
	metadata, err := validateMetadataFields(req.Metadata, true)
	if err != nil {
		return nil, common.CodeArgumentError, err
	}
	var result map[string]any
	code := common.CodeServerError
	err = dao.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		// Match dataset metadata management: active tenant/membership and creator
		// or owner/admin. TEAM visibility alone never grants write permission.
		var kb entity.Knowledgebase
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Select("id").Where("id = ? AND status = ?", id, "1").
			Where("EXISTS (SELECT 1 FROM t_ai_user_tenants m WHERE m.tenant_id = t_ai_knowledgebases.tenant_id AND m.user_id = ? AND m.status = ? AND m.role IN ('owner','admin','normal'))", userID, "1").
			Where("EXISTS (SELECT 1 FROM t_ai_tenants t WHERE t.id = t_ai_knowledgebases.tenant_id AND t.status = ?)", "1").
			Where("EXISTS (SELECT 1 FROM t_ai_users u WHERE u.id = ? AND u.status = ? AND u.is_active = ?)", userID, "1", true).
			Where("created_by = ? OR EXISTS (SELECT 1 FROM t_ai_user_tenants m WHERE m.tenant_id = t_ai_knowledgebases.tenant_id AND m.user_id = ? AND m.status = ? AND m.role IN ('owner','admin'))", userID, userID, "1").Take(&kb).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				code = common.CodeForbidden
			}
			return err
		}
		doc, err := s.documentDAO.GetByDocumentIDAndDatasetID(tx, documentID, kb.ID)
		if err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				code = common.CodeNotFound
			}
			return err
		}
		doc.ParserConfig["metadata"] = metadata
		now := time.Now()
		stamp := now.UnixMilli()
		if doc.UpdateTime != nil && stamp <= *doc.UpdateTime {
			stamp = *doc.UpdateTime + 1
		}
		write := tx.Model(&entity.Document{}).Where("id = ? AND kb_id = ? AND status = ?", doc.ID, kb.ID, "1").Updates(map[string]any{"parser_config": doc.ParserConfig, "update_time": stamp, "update_date": now})
		if write.Error != nil {
			return write.Error
		}
		if write.RowsAffected != 1 {
			return errors.New("document update unavailable")
		}
		// Read back inside the transaction: a failed projection cannot commit an
		// unreported write. The DAO preserves numbers on this read too.
		doc, err = s.documentDAO.GetByDocumentIDAndDatasetID(tx, documentID, kb.ID)
		if err != nil {
			return err
		}
		result = mapDocumentListItem(&entity.DocumentListItem{Document: *doc})
		return nil
	})
	if err != nil {
		switch code {
		case common.CodeForbidden:
			return nil, code, ErrDatasetAccessDenied
		case common.CodeNotFound:
			return nil, code, errors.New("Document not found in dataset.")
		default:
			return nil, common.CodeServerError, errors.New("Unable to update document metadata configuration.")
		}
	}
	return result, common.CodeSuccess, nil
}
