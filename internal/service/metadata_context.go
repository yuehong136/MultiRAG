package service

import (
	"context"
	"fmt"

	"multirag/internal/engine/types"
)

// SearchMetadataContext reads the metadata store's document id field within one
// explicit tenant/dataset. Metadata rows have no chunk availability predicate.
func (s *MetadataService) SearchMetadataContext(ctx context.Context, kbID, tenantID string, docIDs []string, size int) (*SearchMetadataResult, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if kbID == "" || tenantID == "" || size <= 0 {
		return nil, fmt.Errorf("document metadata scope and positive size are required")
	}
	index := BuildMetadataIndexName(tenantID)
	if docIDs != nil && len(docIDs) == 0 {
		return &SearchMetadataResult{IndexName: index, Chunks: []map[string]interface{}{}}, nil
	}
	if s.docEngine == nil {
		return nil, fmt.Errorf("metadata index is unavailable")
	}
	var filter map[string]interface{}
	if docIDs != nil {
		filter = map[string]interface{}{"id": cloneDocumentIDs(docIDs)}
	}
	result, err := s.docEngine.Search(ctx, &types.SearchRequest{
		IndexNames: []string{index}, KbIDs: []string{kbID}, Limit: size,
		SelectFields: []string{"id", "kb_id", "meta_fields"},
		Filter:       filter, IncludeUnavailable: true,
	})
	if err != nil {
		return nil, fmt.Errorf("document metadata search failed: %w", err)
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if result == nil {
		return nil, fmt.Errorf("metadata index returned no result")
	}
	if result.Total > int64(len(result.Chunks)) {
		return nil, fmt.Errorf("document metadata search returned an incomplete selection")
	}
	return &SearchMetadataResult{IndexName: index, Chunks: result.Chunks}, nil
}
