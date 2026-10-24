package service

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"

	"gorm.io/gorm"
	"multirag/internal/common"
	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
)

// FlattenMetadata authorizes the complete scope before reading any document store.
func (s *DatasetsService) FlattenMetadata(ctx context.Context, ids []string, userID string, store engine.DocEngine) (map[string]any, common.ErrorCode, error) {
	if len(ids) == 0 || len(ids) > 100 {
		return nil, common.CodeDataError, fmt.Errorf("between 1 and 100 dataset IDs are required")
	}
	seen := map[string]bool{}
	datasets := []*entity.Knowledgebase{}
	for _, raw := range ids {
		id, err := normalizeDatasetUUID1(strings.TrimSpace(raw))
		if err != nil || seen[id] {
			return nil, common.CodeDataError, fmt.Errorf("distinct valid dataset IDs are required")
		}
		seen[id] = true
		kb, err := s.kbDAO.GetReadableByID(ctx, id, userID)
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, common.CodeForbidden, ErrDatasetAccessDenied
		}
		if err != nil {
			return nil, common.CodeServerError, fmt.Errorf("authorize dataset: %w", err)
		}
		if kb.ID == "" || strings.TrimSpace(kb.TenantID) == "" {
			return nil, common.CodeServerError, fmt.Errorf("authorized dataset identity is missing")
		}
		datasets = append(datasets, kb)
	}
	if store == nil {
		return nil, common.CodeServerError, fmt.Errorf("document engine is not initialized")
	}
	result := map[string]any{}
	for _, kb := range datasets {
		seenDocs := map[string]bool{}
		for offset := 0; ; {
			page, err := store.Search(ctx, &types.SearchRequest{IndexNames: []string{BuildMetadataIndexName(kb.TenantID)}, KbIDs: []string{kb.ID}, Filter: map[string]any{"kb_id": kb.ID}, Offset: offset, Limit: 1000})
			if err != nil {
				return nil, common.CodeServerError, fmt.Errorf("read dataset metadata: %w", err)
			}
			if page == nil || page.Total < 0 || len(page.Chunks) == 0 && int64(offset) < page.Total {
				return nil, common.CodeServerError, fmt.Errorf("invalid metadata result page")
			}
			for _, row := range page.Chunks {
				scopes, _, err := types.ConditionValues(row["kb_id"])
				if err != nil || len(scopes) != 1 || scopes[0] != kb.ID {
					return nil, common.CodeServerError, fmt.Errorf("metadata dataset identity mismatch")
				}
				docID, _ := row["doc_id"].(string)
				if docID == "" {
					docID, _ = row["id"].(string)
				}
				if strings.TrimSpace(docID) == "" || seenDocs[docID] {
					return nil, common.CodeServerError, fmt.Errorf("invalid or duplicate metadata document identity")
				}
				seenDocs[docID] = true
				fields, err := decodeDocumentMetadata(row["meta_fields"])
				if err != nil {
					return nil, common.CodeServerError, err
				}
				for field, raw := range fields {
					values, err := flattenedMetadataValues(raw)
					if err != nil {
						return nil, common.CodeServerError, err
					}
					for _, value := range values {
						if result[field] == nil {
							result[field] = map[string]any{}
						}
						labels := result[field].(map[string]any)
						list, _ := labels[value].([]string)
						found := false
						for _, existing := range list {
							found = found || existing == docID
						}
						if !found {
							list = append(list, docID)
							sort.Strings(list)
							labels[value] = list
						}
					}
				}
			}
			offset += len(page.Chunks)
			if int64(offset) >= page.Total {
				break
			}
			if offset >= 10000 {
				return nil, common.CodeServerError, fmt.Errorf("dataset metadata exceeds 10000 documents")
			}
		}
	}
	return result, common.CodeSuccess, nil
}

func flattenedMetadataValues(raw any) ([]string, error) {
	items, ok := raw.([]any)
	if !ok {
		items = []any{raw}
	}
	values := []string{}
	for _, value := range items {
		switch value.(type) {
		case nil:
			continue
		case string, float64, int, bool:
			if label := fmt.Sprint(value); label != "" {
				values = append(values, label)
			}
		default:
			return nil, fmt.Errorf("metadata values must be scalar or scalar arrays")
		}
	}
	return values, nil
}
