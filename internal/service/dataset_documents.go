package service

import (
	"context"
	"errors"
	"fmt"
	"math"
	"multirag/internal/common"
	"multirag/internal/entity"
	"strings"

	"gorm.io/gorm"
)

type DatasetDocuments struct {
	Docs     []map[string]any `json:"docs"`
	Total    int64            `json:"total"`
	Page     int              `json:"page"`
	PageSize int              `json:"page_size"`
}

// ListDatasetDocuments applies the current private/team read policy before reading document rows.
// Metadata comes from the authoritative SQL document field, without one engine call per document.
func (s *DocumentService) ListDatasetDocuments(ctx context.Context, id, user string, page, pageSize int, keywords string, emptyMetadata bool) (*DatasetDocuments, common.ErrorCode, error) {
	id, err := normalizeDatasetUUID1(strings.TrimSpace(id))
	if err != nil {
		return nil, common.CodeBadRequest, err
	}
	if page < 1 || pageSize < 1 || pageSize > 100 || page > (math.MaxInt/pageSize) || len(keywords) > 1024 {
		return nil, common.CodeBadRequest, fmt.Errorf("invalid document pagination or keywords")
	}
	if _, err := s.kbDAO.GetReadableByID(ctx, id, user); err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, common.CodeForbidden, ErrDatasetAccessDenied
		}
		return nil, common.CodeServerError, fmt.Errorf("dataset read failed")
	}
	query := s.db.WithContext(ctx).Model(&entity.Document{}).Where("kb_id = ? AND status = ?", id, "1")
	if keywords != "" {
		query = query.Where("name LIKE ?", "%"+keywords+"%")
	}
	var total int64
	if err := query.Count(&total).Error; err != nil {
		return nil, common.CodeServerError, fmt.Errorf("document count failed")
	}
	var documents []entity.Document
	if err := query.Order("create_time DESC").Order("id ASC").Offset((page - 1) * pageSize).Limit(pageSize).Find(&documents).Error; err != nil {
		return nil, common.CodeServerError, fmt.Errorf("document list failed")
	}
	result := &DatasetDocuments{Docs: make([]map[string]any, 0, len(documents)), Total: total, Page: page, PageSize: pageSize}
	for _, doc := range documents {
		row := map[string]any{"id": doc.ID, "name": doc.Name, "size": doc.Size, "type": doc.Type, "status": doc.Status, "created_at": doc.CreateTime, "parser_id": doc.ParserID, "progress": doc.Progress, "run": doc.Run}
		if doc.MetaFields != nil && *doc.MetaFields != nil {
			row["meta_fields"] = *doc.MetaFields
		} else if emptyMetadata {
			row["meta_fields"] = map[string]any{}
		}
		result.Docs = append(result.Docs, row)
	}
	return result, common.CodeSuccess, nil
}
