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
	metadata := &MetadataService{docEngine: store}
	for _, kb := range datasets {
		rows, err := metadata.searchMetadataDirectory(ctx, kb.TenantID, []string{kb.ID}, 1000)
		if err != nil {
			return nil, common.CodeServerError, fmt.Errorf("read dataset metadata: %w", err)
		}
		for _, row := range rows {
			docID := row["id"].(string) // The complete directory validates every identity.
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
	}
	return result, common.CodeSuccess, nil
}

func flattenedMetadataValues(raw any) ([]string, error) {
	items, ok := raw.([]any)
	if !ok {
		items = []any{raw}
		if list, ok := raw.([]string); ok {
			items = make([]any, len(list))
			for index, item := range list {
				items[index] = item
			}
		}
	}
	values := []string{}
	for _, value := range items {
		if value == nil {
			continue
		}
		label, valid := metadataValueText(value)
		if !valid {
			return nil, fmt.Errorf("metadata values must be scalar or scalar arrays")
		}
		if label != "" {
			values = append(values, label)
		}
	}
	return values, nil
}
