package service

import (
	"context"
	"errors"
	"fmt"
	"math"
	"multirag/internal/common"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/server"
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
// Configured metadata engines are read once per page; standalone SQL mode reads the document field.
func (s *DocumentService) ListDatasetDocuments(ctx context.Context, id, user string, page, pageSize int, keywords string, emptyMetadata bool) (*DatasetDocuments, common.ErrorCode, error) {
	id, err := normalizeDatasetUUID1(strings.TrimSpace(id))
	if err != nil {
		return nil, common.CodeBadRequest, err
	}
	if page < 1 || pageSize < 1 || pageSize > 100 || page > (math.MaxInt/pageSize) || len(keywords) > 1024 {
		return nil, common.CodeBadRequest, fmt.Errorf("invalid document pagination or keywords")
	}
	kb, err := s.kbDAO.GetReadableByID(ctx, id, user)
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, common.CodeForbidden, ErrDatasetAccessDenied
		}
		return nil, common.CodeServerError, fmt.Errorf("dataset read failed")
	}
	query := s.db.WithContext(ctx).Model(&entity.Document{}).Table("t_ai_documents AS document").Where("document.kb_id = ? AND document.status = ?", id, "1")
	if keywords != "" {
		query = query.Where("document.name LIKE ?", "%"+keywords+"%")
	}
	var total int64
	if err := query.Count(&total).Error; err != nil {
		return nil, common.CodeServerError, fmt.Errorf("document count failed")
	}
	var rows []entity.DocumentListItem
	if err := query.Select("document.*, pipeline.title AS pipeline_name, author.nickname AS nickname").
		Joins("LEFT JOIN t_ai_user_canvases AS pipeline ON pipeline.id = document.pipeline_id AND pipeline.user_id = ?", kb.TenantID).
		Joins("LEFT JOIN t_ai_users AS author ON author.id = document.created_by").
		Order("document.create_time DESC").Order("document.id ASC").Offset((page - 1) * pageSize).Limit(pageSize).Scan(&rows).Error; err != nil {
		return nil, common.CodeServerError, fmt.Errorf("document list failed")
	}
	documents := make([]entity.Document, len(rows))
	for i := range rows {
		documents[i] = rows[i].Document
	}
	result := &DatasetDocuments{Docs: make([]map[string]any, 0, len(documents)), Total: total, Page: page, PageSize: pageSize}
	metadata, err := s.datasetDocumentMetadata(ctx, kb, documents)
	if err != nil {
		return nil, common.CodeServerError, err
	}
	for _, doc := range rows {
		row := mapDocumentListItem(&doc)
		if len(metadata[doc.ID]) > 0 {
			row["meta_fields"] = metadata[doc.ID]
		} else if emptyMetadata {
			row["meta_fields"] = map[string]any{}
		}
		result.Docs = append(result.Docs, row)
	}
	return result, common.CodeSuccess, nil
}

func (s *DocumentService) datasetDocumentMetadata(ctx context.Context, kb *entity.Knowledgebase, documents []entity.Document) (map[string]map[string]any, error) {
	metadata := make(map[string]map[string]any, len(documents))
	if len(documents) == 0 {
		return metadata, nil
	}
	if s.docEngine == nil {
		if s.engineType != server.EngineType("") {
			return nil, fmt.Errorf("configured document metadata engine is unavailable")
		}
		for _, doc := range documents {
			if doc.MetaFields != nil {
				metadata[doc.ID] = *doc.MetaFields
			}
		}
		return metadata, nil
	}
	ids := make([]string, 0, len(documents))
	allowed := map[string]bool{}
	for _, doc := range documents {
		ids = append(ids, doc.ID)
		allowed[doc.ID] = true
	}
	result, err := s.docEngine.Search(ctx, &types.SearchRequest{IndexNames: []string{BuildMetadataIndexName(kb.TenantID)}, KbIDs: []string{kb.ID}, Limit: len(ids), SelectFields: []string{"id", "kb_id", "meta_fields"}, Filter: map[string]any{"id": ids}})
	if err != nil || result == nil {
		return nil, fmt.Errorf("document metadata read failed")
	}
	for _, chunk := range result.Chunks {
		id, ok := chunk["id"].(string)
		if !ok || !allowed[id] || chunk["kb_id"] != kb.ID {
			return nil, fmt.Errorf("metadata document scope mismatch")
		}
		if _, duplicate := metadata[id]; duplicate {
			return nil, fmt.Errorf("duplicate document metadata")
		}
		fields, err := decodeDocumentMetadata(chunk["meta_fields"])
		if err != nil {
			return nil, fmt.Errorf("invalid document metadata")
		}
		metadata[id] = fields
	}
	return metadata, ctx.Err()
}
