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
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"strconv"

	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/engine/types"
)

// KBDocIDsMap maps a KB ID to its document IDs.
// Example: {"kb1": ["doc1", "doc2"], "kb2": ["doc3"]}
type KBDocIDsMap map[string][]string

// DocMetaMap maps a document ID to its metadata fields.
// Example: {"doc1": {"author": "Zhang San", "date": "2024-01-01"}}
type DocMetaMap map[string]map[string]interface{}

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
	return dao.GetTenantIDByKBID(kbID)
}

// GetTenantIDByKBIDs retrieves tenant ID from the first knowledge base ID in the list
func (s *MetadataService) GetTenantIDByKBIDs(kbIDs []string) (string, error) {
	if len(kbIDs) == 0 {
		return "", fmt.Errorf("no kb_ids provided")
	}
	return dao.GetTenantIDByKBID(kbIDs[0])
}

// SearchMetadataResponse holds the result of a metadata search
type SearchMetadataResponse struct {
	IndexName       string
	MetadataRecords []map[string]interface{}
}

// SearchMetadata searches the metadata index with the given parameters
func (s *MetadataService) SearchMetadata(kbID, tenantID string, docIDs []string, size int) (*SearchMetadataResponse, error) {
	if s.docEngine == nil {
		return nil, fmt.Errorf("document engine is not initialized")
	}
	if docIDs != nil && len(docIDs) == 0 {
		return &SearchMetadataResponse{IndexName: BuildMetadataIndexName(tenantID), MetadataRecords: []map[string]interface{}{}}, nil
	}
	searchReq := &types.SearchMetadataRequest{
		TenantID: tenantID,
		Offset:   0,
		Limit:    size,
		Filter: map[string]interface{}{
			"id":    docIDs,
			"kb_id": kbID,
		},
	}

	searchResult, err := s.docEngine.SearchMetadata(context.Background(), searchReq)
	if err != nil {
		return nil, fmt.Errorf("search failed: %w", err)
	}

	if searchResult == nil {
		return nil, fmt.Errorf("metadata search returned no response")
	}
	selected := map[string]bool{}
	for _, id := range docIDs {
		selected[id] = true
	}
	seen := map[string]bool{}
	for _, row := range searchResult.MetadataRecords {
		id := extractDocID(row)
		if id == "" || seen[id] {
			return nil, fmt.Errorf("invalid metadata document identity")
		}
		seen[id] = true
		if docIDs != nil && !selected[id] {
			return nil, fmt.Errorf("metadata escaped selected documents")
		}
		if kb, exists := row["kb_id"]; exists {
			if value, ok := kb.(string); !ok || value != kbID {
				return nil, fmt.Errorf("metadata escaped selected dataset")
			}
		}
	}
	return &SearchMetadataResponse{
		IndexName:       BuildMetadataIndexName(tenantID),
		MetadataRecords: searchResult.MetadataRecords,
	}, nil
}

// SearchMetadataByKBs searches the metadata index for multiple knowledge bases
func (s *MetadataService) SearchMetadataByKBs(kbIDs []string, size int) (*SearchMetadataResponse, error) {
	if len(kbIDs) == 0 {
		return &SearchMetadataResponse{MetadataRecords: []map[string]interface{}{}}, nil
	}

	tenantID, err := s.GetTenantIDByKBIDs(kbIDs)
	if err != nil {
		return nil, err
	}

	if s.docEngine == nil {
		return nil, fmt.Errorf("document engine is not initialized")
	}
	searchReq := &types.SearchMetadataRequest{
		TenantID: tenantID,
		Offset:   0,
		Limit:    size,
		Filter: map[string]interface{}{
			"kb_id": kbIDs,
		},
	}

	searchResult, err := s.docEngine.SearchMetadata(context.Background(), searchReq)
	if err != nil {
		return nil, fmt.Errorf("search failed: %w", err)
	}

	if searchResult == nil {
		return nil, fmt.Errorf("metadata search returned no response")
	}
	return &SearchMetadataResponse{
		IndexName:       BuildMetadataIndexName(tenantID),
		MetadataRecords: searchResult.MetadataRecords,
	}, nil
}

// GetFlattedMetaByKBs returns flattened metadata in the format:
// {field_name: {value: [doc_ids]}}
func (s *MetadataService) GetFlattedMetaByKBs(kbIDs []string) (common.MetaData, error) {
	if len(kbIDs) == 0 {
		return make(common.MetaData), nil
	}
	result, err := s.SearchMetadataByKBs(kbIDs, 10000)
	if err != nil {
		return nil, err
	}
	allowed := make(map[string]bool, len(kbIDs))
	for _, id := range kbIDs {
		allowed[id] = true
	}
	flattened := make(common.MetaData)
	seen := make(map[string]bool)
	for _, row := range result.MetadataRecords {
		docID := extractDocID(row)
		if docID == "" {
			if id, ok := row["id"].(string); ok {
				docID = id
			}
		}
		if docID == "" || seen[docID] {
			return nil, fmt.Errorf("invalid or duplicate metadata document identity")
		}
		seen[docID] = true
		if kb, exists := row["kb_id"]; exists {
			if id, ok := kb.(string); !ok || !allowed[id] {
				return nil, fmt.Errorf("metadata row escaped requested dataset scope")
			}
		}
		fields, err := ExtractMetaFields(row)
		if err != nil {
			return nil, fmt.Errorf("invalid document metadata: %w", err)
		}
		for key, value := range fields {
			var values []interface{}
			switch v := value.(type) {
			case []interface{}:
				values = v
			case []string:
				for _, item := range v {
					values = append(values, item)
				}
			default:
				values = []interface{}{v}
			}
			for _, scalar := range values {
				var text string
				switch v := scalar.(type) {
				case string:
					text = v
				case json.Number:
					text = v.String()
				case bool:
					text = strconv.FormatBool(v)
				case int, int8, int16, int32, int64, uint, uint8, uint16, uint32, uint64:
					text = fmt.Sprint(v)
				case float64:
					if math.IsNaN(v) || math.IsInf(v, 0) {
						return nil, fmt.Errorf("metadata contains non-finite number")
					}
					text = strconv.FormatFloat(v, 'f', -1, 64)
				case float32:
					if math.IsNaN(float64(v)) || math.IsInf(float64(v), 0) {
						return nil, fmt.Errorf("metadata contains non-finite number")
					}
					text = strconv.FormatFloat(float64(v), 'f', -1, 32)
				case nil:
					continue
				default:
					return nil, fmt.Errorf("unsupported metadata scalar type %T", scalar)
				}
				if flattened[key] == nil {
					flattened[key] = make(common.MetaValueDocs)
				}
				flattened[key][text] = appendDocID(flattened[key][text], docID)
			}
		}
	}
	return flattened, nil
}

// CollectDocIDsByKB collects unique (kb_id, doc_id) pairs from chunks.
func CollectDocIDsByKB(chunks []map[string]interface{}) KBDocIDsMap {
	seen := make(map[string]struct{})
	result := make(KBDocIDsMap)
	for _, chunk := range chunks {
		kbID, _ := chunk["kb_id"].(string)
		docID := extractDocID(chunk)
		if kbID == "" || docID == "" {
			continue
		}
		key := kbID + ":" + docID
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		result[kbID] = append(result[kbID], docID)
	}
	return result
}

// ConvertSearchResultToDocMeta converts SearchMetadataResult chunks into a DocMetaMap.
// Pure function, no dependencies.
func ConvertSearchResultToDocMeta(chunks []map[string]interface{}) DocMetaMap {
	metaByDoc := make(DocMetaMap)
	for _, metaChunk := range chunks {
		docID := extractDocID(metaChunk)
		if docID == "" {
			continue
		}
		metaFields, err := ExtractMetaFields(metaChunk)
		if err != nil || len(metaFields) == 0 {
			continue
		}
		metaByDoc[docID] = metaFields
	}
	return metaByDoc
}

// FetchDocMetaByKB fetches document metadata from ES for each KB.
func (s *MetadataService) FetchDocMetaByKB(docIDsByKB KBDocIDsMap, tenantID string) DocMetaMap {
	metaByDoc := make(DocMetaMap)
	for kbID, docIDs := range docIDsByKB {
		result, err := s.SearchMetadata(kbID, tenantID, docIDs, len(docIDs))
		if err != nil {
			continue
		}
		for docID, meta := range ConvertSearchResultToDocMeta(result.MetadataRecords) {
			metaByDoc[docID] = meta
		}
	}
	return metaByDoc
}

// AttachDocMetaToChunks attaches document metadata to matching chunks in-place.
func AttachDocMetaToChunks(chunks []map[string]interface{}, metaByDoc DocMetaMap, metadataFields []string) {
	filter := make(map[string]struct{}, len(metadataFields))
	for _, f := range metadataFields {
		filter[f] = struct{}{}
	}
	for _, chunk := range chunks {
		docID := extractDocID(chunk)
		meta, ok := metaByDoc[docID]
		if !ok {
			continue
		}
		if len(filter) > 0 {
			filtered := make(map[string]interface{}, len(filter))
			for k, v := range meta {
				if _, ok := filter[k]; ok {
					filtered[k] = v
				}
			}
			if len(filtered) > 0 {
				chunk["document_metadata"] = filtered
			}
		} else {
			chunk["document_metadata"] = meta
		}
	}
}

// EnrichChunksWithDocMetadata attaches document metadata to each chunk in-place.
// Combines CollectDocIDsByKB, FetchDocMetaByKB, and AttachDocMetaToChunks.
func (s *MetadataService) EnrichChunksWithDocMetadata(chunks []map[string]interface{}, tenantID string, metadataFields []string) {
	if len(chunks) == 0 || s.docEngine == nil {
		return
	}
	docIDsByKB := CollectDocIDsByKB(chunks)
	if len(docIDsByKB) == 0 {
		return
	}
	metaByDoc := s.FetchDocMetaByKB(docIDsByKB, tenantID)
	if len(metaByDoc) == 0 {
		return
	}
	AttachDocMetaToChunks(chunks, metaByDoc, metadataFields)
}

// extractDocID extracts the document ID from a chunk, checking both id and doc_id.
func extractDocID(chunk map[string]interface{}) string {
	if id, ok := chunk["id"].(string); ok {
		return id
	}
	if id, ok := chunk["doc_id"].(string); ok {
		return id
	}
	return ""
}

// ExtractDocumentID extracts the document ID from a chunk
func ExtractDocumentID(chunk map[string]interface{}) (string, bool) {
	docID, ok := chunk["id"].(string)
	return docID, ok
}

// ExtractMetaFields extracts meta_fields from a chunk, handling different types
func ExtractMetaFields(chunk map[string]interface{}) (map[string]interface{}, error) {
	value := chunk["meta_fields"]
	if value == nil {
		return map[string]interface{}{}, nil
	}
	if fields, ok := value.(map[string]interface{}); ok {
		return fields, nil
	}
	var raw []byte
	switch value := value.(type) {
	case string:
		raw = []byte(value)
	case []byte:
		raw = value
		if len(raw) == 0 {
			return map[string]interface{}{}, nil
		}
	default:
		return nil, fmt.Errorf("invalid metadata value type %T", value)
	}
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 {
		return nil, fmt.Errorf("metadata is empty")
	}
	var objects []map[string]interface{}
	if trimmed[0] == '{' || bytes.Equal(trimmed, []byte("null")) {
		var fields map[string]interface{}
		decoder := json.NewDecoder(bytes.NewReader(trimmed))
		decoder.UseNumber()
		if err := decoder.Decode(&fields); err != nil {
			return nil, fmt.Errorf("invalid metadata JSON: %w", err)
		}
		var trailing interface{}
		if err := decoder.Decode(&trailing); err != io.EOF {
			return nil, fmt.Errorf("trailing metadata JSON")
		}
		if fields == nil {
			fields = map[string]interface{}{}
		}
		objects = []map[string]interface{}{fields}
	} else {
		objects = ParseAllLengthPrefixedJSON(raw)
		if len(objects) == 0 {
			return nil, fmt.Errorf("invalid length-prefixed metadata JSON")
		}
	}
	fields := map[string]interface{}{}
	for _, object := range objects {
		for key, value := range object {
			if existing, ok := fields[key]; ok {
				fields[key] = mergeFieldValues(existing, value)
			} else {
				fields[key] = value
			}
		}
	}
	return fields, nil
}

// mergeFieldValues merges two field values when the same key appears multiple times
// If both are arrays, append all elements. If one is array and other is string, append string to array.
// Returns []interface{} with all merged values (flattened).
func mergeFieldValues(existing, new interface{}) []interface{} {
	result := []interface{}{}

	var addValue func(v interface{})
	addValue = func(v interface{}) {
		if v == nil {
			return
		}
		switch val := v.(type) {
		case string:
			result = append(result, val)
		case []string:
			for _, item := range val {
				addValue(item)
			}
		case []interface{}:
			for _, item := range val {
				addValue(item)
			}
		default:
			result = append(result, val)
		}
	}

	addValue(existing)
	addValue(new)

	return result
}

// appendDocID appends a docID to an existing value that may be []string or []interface{}
func appendDocID(existing interface{}, docID string) []string {
	result := []string{docID}
	if existing == nil {
		return result
	}
	switch v := existing.(type) {
	case []string:
		return append(v, docID)
	case []interface{}:
		for _, item := range v {
			if s, ok := item.(string); ok {
				result = append(result, s)
			}
		}
		return result
	case string:
		return append(result, v)
	}
	return result
}

// ParseLengthPrefixedJSON parses Infinity's length-prefixed JSON format
// Format: [4-byte length (little-endian)][JSON][4-byte length][JSON]...
// Returns the FIRST valid JSON object found
func ParseLengthPrefixedJSON(data []byte) map[string]interface{} {
	values := ParseAllLengthPrefixedJSON(data)
	if len(values) == 0 {
		return nil
	}
	return values[0]
}

// ParseAllLengthPrefixedJSON parses Infinity's length-prefixed JSON format
// and returns ALL JSON objects found (for cases where multiple rows are concatenated)
// Format: [4-byte length (little-endian)][JSON][4-byte length][JSON]...
func ParseAllLengthPrefixedJSON(data []byte) []map[string]interface{} {
	var results []map[string]interface{}
	for offset := 0; offset < len(data); {
		if len(data)-offset < 4 {
			return nil
		}
		length := int(binary.LittleEndian.Uint32(data[offset : offset+4]))
		offset += 4
		if length <= 0 || length > len(data)-offset {
			return nil
		}
		var object map[string]interface{}
		decoder := json.NewDecoder(bytes.NewReader(data[offset : offset+length]))
		decoder.UseNumber()
		if err := decoder.Decode(&object); err != nil || object == nil {
			return nil
		}
		var trailing interface{}
		if err := decoder.Decode(&trailing); err != io.EOF {
			return nil
		}
		results = append(results, object)
		offset += length
	}
	return results
}
