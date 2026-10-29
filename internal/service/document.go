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
	"errors"
	"fmt"
	"multirag/internal/engine/types"
	"regexp"
	"sort"
	"strings"

	"multirag/internal/cache"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/storage"

	"gorm.io/gorm"
)

// DocumentService document service
type DocumentService struct {
	deletion    *DocumentDeleteService
	stopping    *DocumentStopService
	db          *gorm.DB
	documentDAO *dao.DocumentDAO
	kbDAO       *dao.KnowledgebaseDAO
	docEngine   engine.DocEngine
	engineType  server.EngineType
	metadataSvc *MetadataService
}

// NewDocumentService create document service
func NewDocumentService(documentEngines ...engine.DocEngine) *DocumentService {
	store := engine.Get()
	if len(documentEngines) > 0 {
		store = documentEngines[0]
	}
	cfg := server.GetConfig()
	var engineType server.EngineType
	if cfg != nil {
		engineType = cfg.DocEngine.Type
	}
	return &DocumentService{
		db:          dao.DB,
		documentDAO: dao.NewDocumentDAO(),
		kbDAO:       dao.NewKnowledgebaseDAO(),
		docEngine:   store,
		engineType:  engineType,
		metadataSvc: &MetadataService{kbDAO: dao.NewKnowledgebaseDAO(), docEngine: store},
	}
}

// CreateDocumentRequest create document request
type CreateDocumentRequest struct {
	Name      string `json:"name" binding:"required"`
	KbID      string `json:"kb_id" binding:"required"`
	ParserID  string `json:"parser_id" binding:"required"`
	CreatedBy string `json:"created_by" binding:"required"`
	Type      string `json:"type"`
	Source    string `json:"source"`
}

// UpdateDocumentRequest update document request
type UpdateDocumentRequest struct {
	Name        *string  `json:"name"`
	Run         *string  `json:"run"`
	TokenNum    *int64   `json:"token_num"`
	ChunkNum    *int64   `json:"chunk_num"`
	Progress    *float64 `json:"progress"`
	ProgressMsg *string  `json:"progress_msg"`
}

// DocumentResponse document response
type DocumentResponse struct {
	ID              string  `json:"id"`
	Name            *string `json:"name,omitempty"`
	KbID            string  `json:"kb_id"`
	ParserID        string  `json:"parser_id"`
	PipelineID      *string `json:"pipeline_id,omitempty"`
	Type            string  `json:"type"`
	SourceType      string  `json:"source_type"`
	CreatedBy       string  `json:"created_by"`
	Location        *string `json:"location,omitempty"`
	Size            int64   `json:"size"`
	TokenNum        int64   `json:"token_num"`
	ChunkNum        int64   `json:"chunk_num"`
	Progress        float64 `json:"progress"`
	ProgressMsg     *string `json:"progress_msg,omitempty"`
	ProcessDuration float64 `json:"process_duration"`
	Suffix          string  `json:"suffix"`
	Run             *string `json:"run,omitempty"`
	Status          *string `json:"status,omitempty"`
	CreatedAt       string  `json:"created_at"`
	UpdatedAt       string  `json:"updated_at"`
}

// CreateDocument create document
func (s *DocumentService) CreateDocument(req *CreateDocumentRequest) (*entity.Document, error) {
	document := &entity.Document{
		Name:       &req.Name,
		KbID:       req.KbID,
		ParserID:   req.ParserID,
		CreatedBy:  req.CreatedBy,
		Type:       req.Type,
		SourceType: req.Source,
		Suffix:     ".doc",
		Status:     func() *string { s := "0"; return &s }(),
	}

	if err := s.documentDAO.Create(document); err != nil {
		return nil, fmt.Errorf("failed to create document: %w", err)
	}

	return document, nil
}

// GetDocumentByID get document by ID
func (s *DocumentService) GetDocumentByID(id string) (*DocumentResponse, error) {
	document, err := s.documentDAO.GetByID(id)
	if err != nil {
		return nil, err
	}

	return s.toResponse(document), nil
}

// UpdateDocument update document
func (s *DocumentService) UpdateDocument(id string, req *UpdateDocumentRequest) error {
	document, err := s.documentDAO.GetByID(id)
	if err != nil {
		return err
	}

	if req.Name != nil {
		document.Name = req.Name
	}
	if req.Run != nil {
		document.Run = req.Run
	}
	if req.TokenNum != nil {
		document.TokenNum = *req.TokenNum
	}
	if req.ChunkNum != nil {
		document.ChunkNum = *req.ChunkNum
	}
	if req.Progress != nil {
		document.Progress = *req.Progress
	}
	if req.ProgressMsg != nil {
		document.ProgressMsg = req.ProgressMsg
	}

	return s.documentDAO.Update(document)
}

// SetDocumentDeleteService supplies explicit lifecycle dependencies before serving.
func (s *DocumentService) SetDocumentDeleteService(deletion *DocumentDeleteService) {
	s.deletion = deletion
}

func (s *DocumentService) DeleteDocuments(ctx context.Context, datasetID, userID string, ids []string, deleteAll bool) (int, error) {
	if s.deletion != nil {
		return s.deletion.DeleteDocuments(ctx, datasetID, userID, ids, deleteAll)
	}
	client := cache.Get()
	if client == nil {
		return 0, ErrDocumentDeleteRetryable
	}
	deletion := NewDocumentDeleteService(s.db, client.GetClient(), s.docEngine, storage.GetStorageFactory().GetStorage(), s.engineType)
	return deletion.DeleteDocuments(ctx, datasetID, userID, ids, deleteAll)
}

// ListDocuments list documents
func (s *DocumentService) ListDocuments(page, pageSize int) ([]*DocumentResponse, int64, error) {
	offset := (page - 1) * pageSize
	documents, total, err := s.documentDAO.List(offset, pageSize)
	if err != nil {
		return nil, 0, err
	}

	responses := make([]*DocumentResponse, len(documents))
	for i, doc := range documents {
		responses[i] = s.toResponse(doc)
	}

	return responses, total, nil
}

// GetDocumentsByAuthorID get documents by author ID
func (s *DocumentService) GetDocumentsByAuthorID(authorID, page, pageSize int) ([]*DocumentResponse, int64, error) {
	offset := (page - 1) * pageSize
	documents, total, err := s.documentDAO.GetByAuthorID(fmt.Sprintf("%d", authorID), offset, pageSize)
	if err != nil {
		return nil, 0, err
	}

	responses := make([]*DocumentResponse, len(documents))
	for i, doc := range documents {
		responses[i] = s.toResponse(doc)
	}

	return responses, total, nil
}

// toResponse convert entity.Document to DocumentResponse
func (s *DocumentService) toResponse(doc *entity.Document) *DocumentResponse {
	createdAt := ""
	if doc.CreateTime != nil {
		createdAt = entity.TimeFromTimestamp(*doc.CreateTime).Format("2006-01-02 15:04:05")
	}
	updatedAt := ""
	if doc.UpdateTime != nil {
		updatedAt = entity.TimeFromTimestamp(*doc.UpdateTime).Format("2006-01-02 15:04:05")
	}
	return &DocumentResponse{
		ID:              doc.ID,
		Name:            doc.Name,
		KbID:            doc.KbID,
		ParserID:        doc.ParserID,
		PipelineID:      doc.PipelineID,
		Type:            doc.Type,
		SourceType:      doc.SourceType,
		CreatedBy:       doc.CreatedBy,
		Location:        doc.Location,
		Size:            doc.Size,
		TokenNum:        doc.TokenNum,
		ChunkNum:        doc.ChunkNum,
		Progress:        doc.Progress,
		ProgressMsg:     doc.ProgressMsg,
		ProcessDuration: doc.ProcessDuration,
		Suffix:          doc.Suffix,
		Run:             doc.Run,
		Status:          doc.Status,
		CreatedAt:       createdAt,
		UpdatedAt:       updatedAt,
	}
}

// ListDocumentsByKBID reads only active documents after fresh actor/dataset authorization.
func (s *DocumentService) ListDocumentsByKBID(ctx context.Context, kbID, userID string, page, pageSize int) ([]*DocumentResponse, int64, error) {
	if _, err := s.readableMetadataKB(ctx, kbID, userID); err != nil {
		return nil, 0, err
	}
	if page < 1 || pageSize < 1 || pageSize > 100 {
		return nil, 0, ErrInvalidChunkScope
	}
	query := s.db.WithContext(ctx).Model(&entity.Document{}).Where("kb_id=? AND status=?", kbID, string(entity.StatusValid))
	var total int64
	if err := query.Count(&total).Error; err != nil {
		return nil, 0, err
	}
	var documents []entity.Document
	if err := query.Order("id").Offset((page - 1) * pageSize).Limit(pageSize).Find(&documents).Error; err != nil {
		return nil, 0, err
	}
	responses := make([]*DocumentResponse, 0, len(documents))
	for i := range documents {
		responses = append(responses, s.toResponse(&documents[i]))
	}
	return responses, total, ctx.Err()
}

// readableMetadataKB uses the DAO's active tenant/membership predicate and checks
// the current actor's account state before any metadata index is read.
func (s *DocumentService) readableMetadataKB(ctx context.Context, kbID, userID string) (*entity.Knowledgebase, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if strings.TrimSpace(kbID) == "" || strings.TrimSpace(userID) == "" {
		return nil, ErrDatasetAccessDenied
	}
	if s.db == nil || s.kbDAO == nil {
		return nil, fmt.Errorf("metadata SQL is unavailable")
	}
	kb, err := s.kbDAO.GetReadableByID(ctx, kbID, userID)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, ErrDatasetAccessDenied
	}
	if err != nil {
		return nil, err
	}
	var actor entity.User
	err = s.db.WithContext(ctx).Select("id").Where("id=? AND status=? AND is_active=?", userID, string(entity.StatusValid), true).Take(&actor).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, ErrDatasetAccessDenied
	}
	if err != nil {
		return nil, err
	}
	if kb.ID != kbID || strings.TrimSpace(kb.TenantID) == "" {
		return nil, fmt.Errorf("metadata dataset identity is missing")
	}
	return kb, nil
}

// scopedMetadataRows validates the complete SQL document selection before the
// index call and checks every returned row before decoding any metadata.
func (s *DocumentService) scopedMetadataRows(ctx context.Context, kb *entity.Knowledgebase, docIDs []string) ([]map[string]interface{}, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if docIDs != nil && len(docIDs) == 0 {
		return []map[string]interface{}{}, nil
	}
	query := s.db.WithContext(ctx).Model(&entity.Document{}).Where("kb_id=? AND status=?", kb.ID, string(entity.StatusValid))
	selected := map[string]bool{}
	if docIDs != nil {
		for _, id := range docIDs {
			if strings.TrimSpace(id) == "" {
				return nil, ErrInvalidChunkScope
			}
			selected[id] = true
		}
		query = query.Where("id IN ?", docIDs)
	}
	var activeIDs []string
	if err := query.Order("id").Pluck("id", &activeIDs).Error; err != nil {
		return nil, err
	}
	if docIDs != nil && len(activeIDs) != len(selected) {
		return nil, ErrChunkNotFound
	}
	if len(activeIDs) == 0 {
		return []map[string]interface{}{}, nil
	}
	allowed := make(map[string]bool, len(activeIDs))
	for _, id := range activeIDs {
		allowed[id] = true
	}
	if s.metadataSvc == nil {
		return nil, fmt.Errorf("document metadata service is unavailable")
	}
	var found *SearchMetadataResult
	var err error
	if docIDs == nil {
		// Keep the metadata directory's complete paged scan for an all-documents
		// request. SQL's active selection filters stale directory rows below.
		found, err = s.metadataSvc.SearchMetadataByKBsContext(ctx, []string{kb.ID}, 1000)
	} else {
		found, err = s.metadataSvc.SearchMetadataContext(ctx, kb.ID, kb.TenantID, activeIDs, len(activeIDs))
	}
	if err != nil {
		return nil, err
	}
	if found == nil {
		return nil, fmt.Errorf("document metadata result is unavailable")
	}
	rows := make([]map[string]interface{}, 0, len(found.Chunks))
	seen := map[string]bool{}
	for _, row := range found.Chunks {
		id, ok := row["id"].(string)
		if !ok || id == "" || row["kb_id"] != kb.ID || seen[id] {
			return nil, fmt.Errorf("document metadata scope mismatch")
		}
		seen[id] = true
		if !allowed[id] {
			if docIDs == nil {
				continue
			}
			return nil, fmt.Errorf("document metadata scope mismatch")
		}
		fields, err := decodeDocumentMetadata(row["meta_fields"])
		if err != nil {
			return nil, err
		}
		rows = append(rows, map[string]interface{}{"id": id, "kb_id": kb.ID, "meta_fields": fields})
	}
	return rows, ctx.Err()
}

// GetMetadataSummaryRequest request for metadata summary
type GetMetadataSummaryRequest struct {
	KBID   string   `json:"kb_id" binding:"required"`
	DocIDs []string `json:"doc_ids"`
}

// GetMetadataSummaryResponse response for metadata summary
type GetMetadataSummaryResponse struct {
	Summary map[string]interface{} `json:"summary"`
}

// GetMetadataSummary authorizes the full selection and preserves nil versus empty.
func (s *DocumentService) GetMetadataSummary(ctx context.Context, kbID, userID string, docIDs []string) (map[string]interface{}, error) {
	kb, err := s.readableMetadataKB(ctx, kbID, userID)
	if err != nil {
		return nil, err
	}
	rows, err := s.scopedMetadataRows(ctx, kb, docIDs)
	if err != nil {
		return nil, err
	}
	return aggregateMetadata(rows), nil
}

// metadataScope authorizes SQL identities before touching the document engine.
func (s *DocumentService) metadataScope(ctx context.Context, docID, userID string) (*entity.Document, *entity.Knowledgebase, error) {
	if strings.TrimSpace(docID) == "" {
		return nil, nil, ErrInvalidChunkScope
	}
	var doc entity.Document
	err := s.db.WithContext(ctx).Where("id = ? AND status = ?", docID, string(entity.StatusValid)).First(&doc).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, nil, ErrChunkNotFound
	}
	if err != nil {
		return nil, nil, err
	}
	kb, err := s.readableMetadataKB(ctx, doc.KbID, userID)
	if err != nil {
		return nil, nil, err
	}
	if s.docEngine == nil || strings.TrimSpace(kb.TenantID) == "" {
		return nil, nil, fmt.Errorf("document engine or tenant identity is missing")
	}
	return &doc, kb, nil
}

// SetDocumentMetadata merges keys after SQL authorization.
func (s *DocumentService) SetDocumentMetadata(ctx context.Context, docID, userID string, meta map[string]interface{}) error {
	doc, kb, err := s.metadataScope(ctx, docID, userID)
	if err != nil {
		return err
	}
	return s.docEngine.UpdateMetadata(ctx, doc.ID, kb.ID, meta, kb.TenantID)
}

// DeleteDocumentMetadata uses nil only for an omitted keys selection. An explicit
// empty selection is rejected before SQL or storage and never deletes the row.
func (s *DocumentService) DeleteDocumentMetadata(ctx context.Context, docID, userID string, keys []string) error {
	if keys != nil {
		if err := types.ValidateMetadataKeys(keys); err != nil {
			return fmt.Errorf("%w: %v", ErrInvalidChunkScope, err)
		}
	}
	doc, kb, err := s.metadataScope(ctx, docID, userID)
	if err != nil {
		return err
	}
	if keys != nil {
		return s.docEngine.DeleteMetadataKeys(ctx, doc.ID, kb.ID, keys, kb.TenantID)
	}
	_, err = s.docEngine.DeleteMetadata(ctx, map[string]interface{}{"id": doc.ID, "kb_id": kb.ID}, kb.TenantID)
	return err
}

// GetDocumentMetadataByID validates an active exact-KB document before index access.
func (s *DocumentService) GetDocumentMetadataByID(ctx context.Context, docID, userID string) (map[string]interface{}, error) {
	doc, kb, err := s.metadataScope(ctx, docID, userID)
	if err != nil {
		return nil, err
	}
	rows, err := s.scopedMetadataRows(ctx, kb, []string{doc.ID})
	if err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return map[string]interface{}{}, nil
	}
	return rows[0]["meta_fields"].(map[string]interface{}), nil
}

// GetMetadataByKBs authorizes every dataset before reading the first index.
func (s *DocumentService) GetMetadataByKBs(ctx context.Context, userID string, kbIDs []string) (map[string]interface{}, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	kbs := make([]*entity.Knowledgebase, 0, len(kbIDs))
	seen := map[string]bool{}
	for _, id := range kbIDs {
		if seen[id] {
			continue
		}
		kb, err := s.readableMetadataKB(ctx, id, userID)
		if err != nil {
			return nil, err
		}
		seen[id] = true
		kbs = append(kbs, kb)
	}
	flattened := map[string]map[string][]string{}
	for _, kb := range kbs {
		rows, err := s.scopedMetadataRows(ctx, kb, nil)
		if err != nil {
			return nil, err
		}
		for _, row := range rows {
			docID := row["id"].(string)
			for field, raw := range row["meta_fields"].(map[string]interface{}) {
				if field == "kb_id" || field == "id" {
					continue
				}
				if flattened[field] == nil {
					flattened[field] = map[string][]string{}
				}
				values, ok := raw.([]interface{})
				if !ok {
					values = []interface{}{raw}
					if list, ok := raw.([]string); ok {
						values = make([]interface{}, len(list))
						for i, value := range list {
							values[i] = value
						}
					}
				}
				for _, value := range values {
					if value != nil {
						label := fmt.Sprintf("%v", value)
						flattened[field][label] = append(flattened[field][label], docID)
					}
				}
			}
		}
	}
	result := map[string]interface{}{}
	for field, values := range flattened {
		result[field] = values
	}
	return result, ctx.Err()
}

// valueInfo holds count and order of first appearance
type valueInfo struct {
	count      int
	firstOrder int
}

// aggregateMetadata aggregates metadata from search results
func aggregateMetadata(chunks []map[string]interface{}) map[string]interface{} {
	// summary: map[fieldName]map[value]valueInfo
	summary := make(map[string]map[string]valueInfo)
	typeCounter := make(map[string]map[string]int)
	orderCounter := 0

	for _, chunk := range chunks {
		// For metadata table, the actual metadata is in the "meta_fields" JSON field
		// Extract it first
		metaFieldsVal := chunk["meta_fields"]
		if metaFieldsVal == nil {
			continue
		}

		// Parse meta_fields - could be a string (JSON) or a map
		var metaFields map[string]interface{}
		switch v := metaFieldsVal.(type) {
		case string:
			// Parse JSON string
			if err := json.Unmarshal([]byte(v), &metaFields); err != nil {
				continue
			}
		case []byte:
			// Handle byte slice - Infinity returns concatenated JSON objects with length prefixes
			rawBytes := v

			// Try to detect and handle length-prefixed format
			// Format: [4-byte length][JSON][4-byte length][JSON]...
			parsedMetaFields := make(map[string]interface{})
			offset := 0
			for offset < len(rawBytes) {
				// Need at least 4 bytes for length prefix
				if offset+4 > len(rawBytes) {
					break
				}

				// Read 4-byte length (little-endian, not big-endian!)
				length := uint32(rawBytes[offset]) | uint32(rawBytes[offset+1])<<8 |
					uint32(rawBytes[offset+2])<<16 | uint32(rawBytes[offset+3])<<24

				// Check if length looks valid (not too large)
				if length > 10000 || length == 0 {
					// Try to find next '{' from current position
					nextBrace := -1
					for i := offset; i < len(rawBytes) && i < offset+100; i++ {
						if rawBytes[i] == '{' {
							nextBrace = i
							break
						}
					}
					if nextBrace > offset {
						// Skip to the next '{'
						offset = nextBrace
						continue
					}
					break
				}

				// Extract JSON data
				jsonStart := offset + 4
				jsonEnd := jsonStart + int(length)
				if jsonEnd > len(rawBytes) {
					jsonEnd = len(rawBytes)
				}

				jsonBytes := rawBytes[jsonStart:jsonEnd]

				// Try to parse this JSON
				var singleMeta map[string]interface{}
				if err := json.Unmarshal(jsonBytes, &singleMeta); err == nil {
					// Merge metadata from this document
					for k, vv := range singleMeta {
						if existing, ok := parsedMetaFields[k]; ok {
							// Combine values
							if existList, ok := existing.([]interface{}); ok {
								if newList, ok := vv.([]interface{}); ok {
									parsedMetaFields[k] = append(existList, newList...)
								} else {
									parsedMetaFields[k] = append(existList, vv)
								}
							} else {
								parsedMetaFields[k] = []interface{}{existing, vv}
							}
						} else {
							parsedMetaFields[k] = vv
						}
					}
				}

				offset = jsonEnd
			}

			// If we successfully parsed multiple JSON objects, use the merged result
			if len(parsedMetaFields) > 0 {
				metaFields = parsedMetaFields
			} else {
				// Fallback: try the original parsing method
				startIdx := -1
				for i, b := range rawBytes {
					if b == '{' {
						startIdx = i
						break
					}
				}
				if startIdx > 0 {
					strVal := string(rawBytes[startIdx:])
					if err := json.Unmarshal([]byte(strVal), &metaFields); err != nil {
						metaFields = map[string]interface{}{"raw": strVal}
					}
				} else if err := json.Unmarshal(rawBytes, &metaFields); err != nil {
					metaFields = map[string]interface{}{"raw": string(rawBytes)}
				}
			}
		case map[string]interface{}:
			metaFields = v
		default:
			continue
		}

		// Now iterate over the extracted metadata fields
		for k, v := range metaFields {
			// Skip nil values
			if v == nil {
				continue
			}

			// Determine value type
			valueType := getMetaValueType(v)

			// Track type counts
			if valueType != "" {
				if _, ok := typeCounter[k]; !ok {
					typeCounter[k] = make(map[string]int)
				}
				typeCounter[k][valueType] = typeCounter[k][valueType] + 1
			}

			// Aggregate value counts
			var values interface{} = v
			if vl, ok := v.([]interface{}); ok {
				values = vl
			} else {
				values = []interface{}{v}
			}

			for _, vv := range values.([]interface{}) {
				if vv == nil {
					continue
				}
				sv := fmt.Sprintf("%v", vv)

				if _, ok := summary[k]; !ok {
					summary[k] = make(map[string]valueInfo)
				}

				if existing, ok := summary[k][sv]; ok {
					// Already exists, just increment count
					existing.count++
					summary[k][sv] = existing
				} else {
					// First time seeing this value - record order
					summary[k][sv] = valueInfo{count: 1, firstOrder: orderCounter}
					orderCounter++
				}
			}
		}
	}

	// Build result with type information and sorted values
	result := make(map[string]interface{})
	for k, v := range summary {
		// Sort by count descending, then by firstOrder ascending (to match Python stable sort)
		// values: [value, count, firstOrder]
		values := make([][3]interface{}, 0, len(v))
		for val, info := range v {
			values = append(values, [3]interface{}{val, info.count, info.firstOrder})
		}
		// Use stable sort - sort by count descending, then by firstOrder
		sort.SliceStable(values, func(i, j int) bool {
			cntI := values[i][1].(int)
			cntJ := values[j][1].(int)
			if cntI != cntJ {
				return cntI > cntJ // count descending
			}
			// If counts equal, use firstOrder ascending (earlier appearance first)
			return values[i][2].(int) < values[j][2].(int)
		})

		// Determine dominant type
		valueType := "string"
		if typeCounts, ok := typeCounter[k]; ok {
			maxCount := 0
			for t, c := range typeCounts {
				if c > maxCount {
					maxCount = c
					valueType = t
				}
			}
		}

		// Convert from [value, count, firstOrder] to [value, count] for output
		outputValues := make([][2]interface{}, len(values))
		for i, val := range values {
			outputValues[i] = [2]interface{}{val[0], val[1]}
		}

		result[k] = map[string]interface{}{
			"type":   valueType,
			"values": outputValues,
		}
	}

	return result
}

// getMetaValueType determines the type of a metadata value
func getMetaValueType(value interface{}) string {
	if value == nil {
		return ""
	}

	switch v := value.(type) {
	case []interface{}:
		if len(v) > 0 {
			return "list"
		}
		return ""
	case bool:
		return "string"
	case int, int8, int16, int32, int64:
		return "number"
	case float32, float64, json.Number:
		return "number"
	case string:
		if isTimeString(v) {
			return "time"
		}
		return "string"
	}
	return "string"
}

// isTimeString checks if a string is an ISO 8601 datetime
func isTimeString(s string) bool {
	matched, _ := regexp.MatchString(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$`, s)
	return matched
}
