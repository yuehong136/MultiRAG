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
	metadata, err := s.datasetDocumentMetadata(ctx, kb, documents)
	if err != nil {
		return nil, common.CodeServerError, err
	}
	for _, doc := range documents {
		row := map[string]any{"id": doc.ID, "name": doc.Name, "size": doc.Size, "type": doc.Type, "status": doc.Status, "created_at": doc.CreateTime, "parser_id": doc.ParserID, "progress": doc.Progress, "run": doc.Run}
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
	result, err := s.docEngine.Search(ctx, &types.SearchRequest{IndexNames: []string{BuildMetadataIndexName(kb.TenantID)}, KbIDs: []string{kb.ID}, Limit: len(ids), Filter: map[string]any{"doc_id": ids}})
	if err != nil || result == nil {
		return nil, fmt.Errorf("document metadata read failed")
	}
	for _, chunk := range result.Chunks {
		id, ok := chunk["doc_id"].(string)
		if !ok || !allowed[id] {
			return nil, fmt.Errorf("metadata document scope mismatch")
		}
		if _, duplicate := metadata[id]; duplicate {
			return nil, fmt.Errorf("duplicate document metadata")
		}
		fields, err := ExtractMetaFields(chunk)
		if err != nil {
			return nil, fmt.Errorf("invalid document metadata")
		}
		metadata[id] = fields
	}
	return metadata, ctx.Err()
}
