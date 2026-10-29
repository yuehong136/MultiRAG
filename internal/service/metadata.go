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

package service

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strconv"

	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
)

// MetadataService provides common metadata operations
type MetadataService struct {
	kbDAO     *dao.KnowledgebaseDAO
	docEngine engine.DocEngine
}

// NewMetadataService creates a new metadata service
func NewMetadataService() *MetadataService {
	return &MetadataService{
		kbDAO:     dao.NewKnowledgebaseDAO(),
		docEngine: engine.Get(),
	}
}

// BuildMetadataIndexName constructs the metadata index name for a tenant
func BuildMetadataIndexName(tenantID string) string {
	return fmt.Sprintf("multirag_doc_meta_%s", tenantID)
}

// GetTenantIDByKBID retrieves tenant ID from knowledge base ID
func (s *MetadataService) GetTenantIDByKBID(kbID string) (string, error) {
	kb, err := s.kbDAO.GetByID(kbID)
	if err != nil {
		return "", fmt.Errorf("knowledgebase not found: %w", err)
	}
	return kb.TenantID, nil
}

// GetTenantIDByKBIDs retrieves tenant ID from the first knowledge base ID in the list
func (s *MetadataService) GetTenantIDByKBIDs(kbIDs []string) (string, error) {
	if len(kbIDs) == 0 {
		return "", fmt.Errorf("no kb_ids provided")
	}
	kb, err := s.kbDAO.GetByID(kbIDs[0])
	if err != nil {
		return "", fmt.Errorf("knowledgebase not found: %w", err)
	}
	return kb.TenantID, nil
}

// SearchMetadataResult holds the result of a metadata search
type SearchMetadataResult struct {
	IndexName string
	Chunks    []map[string]interface{}
}

// SearchMetadata searches the metadata index with the given parameters
func (s *MetadataService) SearchMetadata(kbID, tenantID string, docIDs []string, size int) (*SearchMetadataResult, error) {
	return s.SearchMetadataContext(context.Background(), kbID, tenantID, docIDs, size)
}

// SearchMetadataByKBs searches the metadata index for multiple knowledge bases
func (s *MetadataService) SearchMetadataByKBs(kbIDs []string, size int) (*SearchMetadataResult, error) {
	return s.SearchMetadataByKBsContext(context.Background(), kbIDs, size)
}

// SearchMetadataByKBsContext keeps both SQL and index work in the request scope.
func (s *MetadataService) SearchMetadataByKBsContext(ctx context.Context, kbIDs []string, size int) (*SearchMetadataResult, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if len(kbIDs) == 0 {
		return &SearchMetadataResult{Chunks: []map[string]interface{}{}}, nil
	}

	if size <= 0 {
		return nil, fmt.Errorf("positive metadata result size is required")
	}
	// Resolve every selected KB before reading any tenant index.
	var records []entity.Knowledgebase
	if err := dao.DB.WithContext(ctx).Select("id", "tenant_id").Where("id IN ?", kbIDs).Find(&records).Error; err != nil {
		return nil, fmt.Errorf("knowledgebase read failed: %w", err)
	}
	tenants := make(map[string]string, len(records))
	for _, kb := range records {
		tenants[kb.ID] = kb.TenantID
	}
	groups := make(map[string][]string)
	seen := make(map[string]bool)
	for _, id := range kbIDs {
		if tenants[id] == "" {
			return nil, fmt.Errorf("knowledgebase not found")
		}
		if !seen[id] {
			groups[tenants[id]] = append(groups[tenants[id]], id)
			seen[id] = true
		}
	}
	if s.docEngine == nil {
		return nil, fmt.Errorf("metadata index is unavailable")
	}
	ordered := make([]string, 0, len(groups))
	for tenant := range groups {
		ordered = append(ordered, tenant)
	}
	sort.Strings(ordered)
	result := &SearchMetadataResult{Chunks: []map[string]interface{}{}}
	for _, tenant := range ordered {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		index := BuildMetadataIndexName(tenant)
		rows, err := s.searchMetadataDirectory(ctx, tenant, groups[tenant], size)
		if err != nil {
			return nil, err
		}
		result.Chunks = append(result.Chunks, rows...)
		if len(ordered) == 1 {
			result.IndexName = index
		}
	}
	return result, ctx.Err()
}

// searchMetadataDirectory reads a complete, stable tenant selection. The size
// controls batch memory and never limits the directory used by metadata filters.
func (s *MetadataService) searchMetadataDirectory(ctx context.Context, tenant string, kbIDs []string, size int) ([]map[string]interface{}, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if tenant == "" || len(kbIDs) == 0 || size <= 0 || s.docEngine == nil {
		return nil, fmt.Errorf("metadata directory scope, engine and positive batch size are required")
	}
	allowedKBs := make(map[string]bool, len(kbIDs))
	for _, id := range kbIDs {
		if id == "" {
			return nil, fmt.Errorf("metadata directory dataset scope is required")
		}
		allowedKBs[id] = true
	}
	batchSize := min(size, 1000)
	rows := []map[string]interface{}{}
	seen := make(map[string]bool)
	appendRows := func(batch []map[string]interface{}) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		if len(batch) > batchSize {
			return fmt.Errorf("metadata directory returned an overfull page")
		}
		for _, row := range batch {
			kb, kbOK := row["kb_id"].(string)
			id, idOK := row["id"].(string)
			if !kbOK || !allowedKBs[kb] || !idOK || id == "" {
				return fmt.Errorf("metadata directory dataset or document scope mismatch")
			}
			identity := kb + "\x00" + id
			if seen[identity] {
				return fmt.Errorf("metadata directory returned a duplicate document")
			}
			seen[identity] = true
			rows = append(rows, row)
		}
		return nil
	}
	request := &types.SearchRequest{
		IndexNames: []string{BuildMetadataIndexName(tenant)}, KbIDs: append([]string(nil), kbIDs...),
		Limit: batchSize, SelectFields: []string{"id", "kb_id", "meta_fields"}, IncludeUnavailable: true,
		OrderBy: new(types.OrderByExpr).Asc("kb_id").Asc("id"),
	}
	// ES can hold a point-in-time snapshot across its result-window boundary;
	// adapters without that capability still paginate with exact total checks.
	if scanner, ok := s.docEngine.(engine.MetadataScanner); ok {
		if err := scanner.ScanMetadata(ctx, request, appendRows); err != nil {
			return nil, fmt.Errorf("metadata snapshot scan failed: %w", err)
		}
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		return rows, nil
	}
	expectedTotal := int64(-1)
	for {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		request.Offset = len(rows)
		found, err := s.docEngine.Search(ctx, request)
		if err != nil {
			return nil, fmt.Errorf("metadata search failed: %w", err)
		}
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if found == nil {
			return nil, fmt.Errorf("metadata index returned no result")
		}
		if found.Total < 0 {
			return nil, fmt.Errorf("metadata directory returned invalid pagination")
		}
		if expectedTotal < 0 {
			expectedTotal = found.Total
		} else if found.Total != expectedTotal {
			return nil, fmt.Errorf("metadata directory changed during pagination")
		}
		if int64(len(rows))+int64(len(found.Chunks)) > expectedTotal {
			return nil, fmt.Errorf("metadata directory returned more rows than its total")
		}
		if err := appendRows(found.Chunks); err != nil {
			return nil, err
		}
		if int64(len(rows)) == expectedTotal {
			return rows, nil
		}
		if len(found.Chunks) == 0 {
			return nil, fmt.Errorf("metadata directory pagination made no progress")
		}
	}
}

// GetFlattedMetaByKBs returns flattened metadata in the format:
// {field_name: {value: [doc_ids]}}
func (s *MetadataService) GetFlattedMetaByKBs(kbIDs []string) (common.MetaData, error) {
	return s.GetFlattedMetaByKBsContext(context.Background(), kbIDs)
}

// GetFlattedMetaByKBsContext supplies the directory for request-scoped filters.
func (s *MetadataService) GetFlattedMetaByKBsContext(ctx context.Context, kbIDs []string) (common.MetaData, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if len(kbIDs) == 0 {
		return make(common.MetaData), nil
	}

	// Read every metadata row; the batch size is not a result cap.
	result, err := s.SearchMetadataByKBsContext(ctx, kbIDs, 10000)
	if err != nil {
		return nil, err
	}

	return flattenMetadataRows(result.Chunks)
}

func flattenMetadataRows(rows []map[string]interface{}) (common.MetaData, error) {
	flattedMeta := make(common.MetaData)
	for _, chunk := range rows {
		// Extract doc_id from chunk
		docID := ""
		if id, ok := chunk["id"].(string); ok {
			docID = id
		} else if id, ok := chunk["doc_id"].(string); ok {
			docID = id
		}

		if docID == "" {
			return nil, fmt.Errorf("metadata directory document id is required")
		}

		// Corrupt metadata cannot become an incomplete directory that broadens
		// a negative/manual predicate or changes a model-generated filter.
		metaFields, err := ExtractMetaFields(chunk)
		if err != nil {
			return nil, fmt.Errorf("metadata directory contains invalid fields: %w", err)
		}
		if len(metaFields) == 0 {
			continue
		}

		// Flatten each field
		for fieldName, fieldValue := range metaFields {
			if fieldName == "empty_metadata" || fieldValue == nil {
				continue
			}

			// Initialize field map if not exists
			if _, exists := flattedMeta[fieldName]; !exists {
				flattedMeta[fieldName] = make(common.MetaValueDocs)
			}

			valueMap := flattedMeta[fieldName]
			addValue := func(value interface{}) {
				if text, valid := metadataValueText(value); valid {
					valueMap[text] = appendDocID(valueMap[text], docID)
				}
			}

			// Preserve JSON scalars, including false, zero and empty strings.
			switch v := fieldValue.(type) {
			case []string:
				for _, item := range v {
					addValue(item)
				}
			case []interface{}:
				for _, item := range v {
					addValue(item)
				}
			default:
				addValue(v)
			}
		}
	}

	return flattedMeta, nil
}

// metadataValueText preserves scalar spelling without rounding integer values.
func metadataValueText(value interface{}) (string, bool) {
	switch scalar := value.(type) {
	case string:
		return scalar, true
	case bool:
		return strconv.FormatBool(scalar), true
	case int:
		return strconv.Itoa(scalar), true
	case int8:
		return strconv.FormatInt(int64(scalar), 10), true
	case int16:
		return strconv.FormatInt(int64(scalar), 10), true
	case int32:
		return strconv.FormatInt(int64(scalar), 10), true
	case int64:
		return strconv.FormatInt(scalar, 10), true
	case uint:
		return strconv.FormatUint(uint64(scalar), 10), true
	case uint8:
		return strconv.FormatUint(uint64(scalar), 10), true
	case uint16:
		return strconv.FormatUint(uint64(scalar), 10), true
	case uint32:
		return strconv.FormatUint(uint64(scalar), 10), true
	case uint64:
		return strconv.FormatUint(scalar, 10), true
	case float32:
		if !math.IsNaN(float64(scalar)) && !math.IsInf(float64(scalar), 0) {
			return strconv.FormatFloat(float64(scalar), 'f', -1, 32), true
		}
	case float64:
		if !math.IsNaN(scalar) && !math.IsInf(scalar, 0) {
			return strconv.FormatFloat(scalar, 'f', -1, 64), true
		}
	case json.Number:
		if number, err := scalar.Float64(); err == nil && !math.IsNaN(number) && !math.IsInf(number, 0) {
			return scalar.String(), true
		}
	}
	return "", false
}

// ExtractDocumentID extracts the document ID from a chunk
func ExtractDocumentID(chunk map[string]interface{}) (string, bool) {
	docID, ok := chunk["id"].(string)
	return docID, ok
}

// ExtractMetaFields extracts meta_fields from a chunk, handling different types
func ExtractMetaFields(chunk map[string]interface{}) (map[string]interface{}, error) {
	return decodeDocumentMetadata(chunk["meta_fields"])
}

// mergeFieldValues merges two field values when the same key appears multiple times
// Arrays are flattened and all supported JSON scalars are retained.
// Returns []interface{} with all merged values (flattened).
func mergeFieldValues(existing, new interface{}) []interface{} {
	result := []interface{}{}

	var addValue func(v interface{})
	addValue = func(v interface{}) {
		if v == nil {
			return
		}
		switch val := v.(type) {
		case string, bool, float32, float64, int, int8, int16, int32, int64, uint, uint8, uint16, uint32, uint64, json.Number:
			result = append(result, val)
		case []string:
			for _, item := range val {
				addValue(item)
			}
		case []interface{}:
			for _, item := range val {
				addValue(item)
			}
		}
	}

	addValue(existing)
	addValue(new)

	return result
}

// appendDocID appends a document to the typed directory; MetaFilter deduplicates matches.
func appendDocID(existing []string, docID string) []string {
	return append(existing, docID)
}

// ParseLengthPrefixedJSON parses Infinity's length-prefixed JSON format
// Format: [4-byte length (little-endian)][JSON][4-byte length][JSON]...
// Returns the FIRST valid JSON object found
func ParseLengthPrefixedJSON(data []byte) map[string]interface{} {
	if len(data) < 4 {
		return nil
	}

	// Try to find the first valid JSON object by skipping length prefixes
	offset := 0
	for offset < len(data) {
		// Skip non-'{' bytes
		for offset < len(data) && data[offset] != '{' {
			offset++
		}
		if offset >= len(data) {
			break
		}

		// Try to parse JSON from current position
		var result map[string]interface{}
		err := decodeMetadataObject(data[offset:], &result)
		if err == nil {
			return result
		}

		// Move forward to try next position
		offset++
	}
	return nil
}

// ParseAllLengthPrefixedJSON parses Infinity's length-prefixed JSON format
// and returns ALL JSON objects found (for cases where multiple rows are concatenated)
// Format: [4-byte length (little-endian)][JSON][4-byte length][JSON]...
func ParseAllLengthPrefixedJSON(data []byte) []map[string]interface{} {
	if len(data) < 4 {
		return nil
	}

	var results []map[string]interface{}
	offset := 0

	// Use length prefix to extract each JSON
	for offset+4 <= len(data) {
		// Read 4-byte length (little-endian)
		length := uint32(data[offset]) | uint32(data[offset+1])<<8 |
			uint32(data[offset+2])<<16 | uint32(data[offset+3])<<24

		// Check if length looks reasonable
		if length == 0 || offset+4+int(length) > len(data) {
			// Length invalid, try to find next '{'
			nextBrace := -1
			for i := offset + 4; i < len(data) && i < offset+104; i++ {
				if data[i] == '{' {
					nextBrace = i
					break
				}
			}
			if nextBrace > offset {
				offset = nextBrace
				continue
			}
			break
		}

		// Extract JSON bytes (skip the 4-byte length prefix)
		jsonStart := offset + 4
		jsonEnd := jsonStart + int(length)
		jsonBytes := data[jsonStart:jsonEnd]

		var result map[string]interface{}
		if err := decodeMetadataObject(jsonBytes, &result); err == nil {
			results = append(results, result)
			offset = jsonEnd
			continue
		} else {
			// Try to find next '{'
			nextBrace := -1
			for i := offset + 4; i < len(data) && i < offset+104; i++ {
				if data[i] == '{' {
					nextBrace = i
					break
				}
			}
			if nextBrace > offset {
				offset = nextBrace
				continue
			}
			break
		}
	}
	return results
}
