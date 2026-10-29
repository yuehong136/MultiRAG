package service

import (
	"context"
	"fmt"
	"sort"

	"multirag/internal/entity"
)

// KBDocIDsMap collects document identities within each dataset.
type KBDocIDsMap map[string][]string

// DocMetaMap keeps dataset identity alongside document identity. A document ID
// alone is not a safe key when references span datasets or tenants.
type DocMetaMap map[string]map[string]map[string]interface{}

func referenceKBID(value interface{}) string {
	switch id := value.(type) {
	case string:
		return id
	case []string:
		if len(id) == 1 {
			return id[0]
		}
	case []interface{}:
		if len(id) == 1 {
			text, _ := id[0].(string)
			return text
		}
	}
	return ""
}

// CollectDocIDsByKB deduplicates actual chunk doc_id values. The id of a
// retrieval chunk is the chunk identity and must never select document metadata.
func CollectDocIDsByKB(chunks []map[string]interface{}) KBDocIDsMap {
	sets := make(map[string]map[string]bool)
	for _, chunk := range chunks {
		kb := referenceKBID(chunk["kb_id"])
		doc, _ := chunk["doc_id"].(string)
		if kb == "" || doc == "" {
			continue
		}
		if sets[kb] == nil {
			sets[kb] = make(map[string]bool)
		}
		sets[kb][doc] = true
	}
	result := make(KBDocIDsMap, len(sets))
	for kb, documents := range sets {
		for doc := range documents {
			result[kb] = append(result[kb], doc)
		}
		sort.Strings(result[kb])
	}
	return result
}

// ConvertSearchResultToDocMeta validates the metadata store's id/kb_id contract
// against the exact requested pair selection before accepting any fields.
func ConvertSearchResultToDocMeta(kbID string, docIDs []string, chunks []map[string]interface{}) (DocMetaMap, error) {
	allowed := make(map[string]bool, len(docIDs))
	for _, id := range docIDs {
		allowed[id] = true
	}
	result := DocMetaMap{kbID: make(map[string]map[string]interface{})}
	for _, chunk := range chunks {
		id, ok := chunk["id"].(string)
		if !ok || !allowed[id] || chunk["kb_id"] != kbID {
			return nil, fmt.Errorf("document metadata scope mismatch")
		}
		if _, duplicate := result[kbID][id]; duplicate {
			return nil, fmt.Errorf("duplicate document metadata")
		}
		fields, err := decodeDocumentMetadata(chunk["meta_fields"])
		if err != nil {
			return nil, err
		}
		result[kbID][id] = cloneMetadataValue(fields).(map[string]interface{})
	}
	return result, nil
}

// AttachDocMetaToChunks returns independent reference maps, replacing stale
// document_metadata. nil fields selects all; a non-nil empty slice selects none.
func AttachDocMetaToChunks(chunks []map[string]interface{}, metadata DocMetaMap, fields []string) []map[string]interface{} {
	if chunks == nil {
		return nil
	}
	selected := make(map[string]bool, len(fields))
	for _, field := range fields {
		selected[field] = true
	}
	result := make([]map[string]interface{}, len(chunks))
	for index, chunk := range chunks {
		if chunk == nil {
			continue
		}
		copy := cloneMetadataValue(chunk).(map[string]interface{})
		delete(copy, "document_metadata")
		doc, _ := chunk["doc_id"].(string)
		values := metadata[referenceKBID(chunk["kb_id"])][doc]
		attached := make(map[string]interface{})
		for key, value := range values {
			if fields == nil || selected[key] {
				attached[key] = cloneMetadataValue(value)
			}
		}
		if len(attached) > 0 {
			copy["document_metadata"] = attached
		}
		result[index] = copy
	}
	return result
}

type referenceKBResolver func(context.Context, string, string) (*entity.Knowledgebase, error)

// EnrichChunksWithDocMetadata reads document metadata only after authorizing
// every referenced dataset for userID. It returns copies and propagates failures;
// callers decide whether reference preferences enable this optional operation.
func (s *MetadataService) EnrichChunksWithDocMetadata(ctx context.Context, chunks []map[string]interface{}, userID string, fields []string) ([]map[string]interface{}, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if len(chunks) == 0 || fields != nil && len(fields) == 0 {
		return AttachDocMetaToChunks(chunks, nil, fields), nil
	}
	if s.kbDAO == nil {
		return nil, fmt.Errorf("knowledgebase reader is unavailable")
	}
	return s.enrichChunksWithDocMetadata(ctx, chunks, userID, fields, s.kbDAO.GetReadableByID)
}

func (s *MetadataService) enrichChunksWithDocMetadata(ctx context.Context, chunks []map[string]interface{}, userID string, fields []string, resolve referenceKBResolver) ([]map[string]interface{}, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if len(chunks) == 0 || fields != nil && len(fields) == 0 {
		return AttachDocMetaToChunks(chunks, nil, fields), nil
	}
	pairs := CollectDocIDsByKB(chunks)
	keys := make([]string, 0, len(pairs))
	for kb := range pairs {
		keys = append(keys, kb)
	}
	sort.Strings(keys)
	// Resolve the complete selection before making the first index request.
	tenants := make(map[string]string, len(keys))
	for _, id := range keys {
		kb, err := resolve(ctx, id, userID)
		if err != nil {
			return nil, err
		}
		if kb == nil || kb.ID != id || kb.TenantID == "" {
			return nil, fmt.Errorf("invalid document metadata dataset scope")
		}
		tenants[id] = kb.TenantID
	}
	metadata := make(DocMetaMap, len(keys))
	for _, id := range keys {
		result, err := s.SearchMetadataContext(ctx, id, tenants[id], pairs[id], len(pairs[id]))
		if err != nil {
			return nil, err
		}
		converted, err := ConvertSearchResultToDocMeta(id, pairs[id], result.Chunks)
		if err != nil {
			return nil, err
		}
		metadata[id] = converted[id]
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return AttachDocMetaToChunks(chunks, metadata, fields), nil
}
