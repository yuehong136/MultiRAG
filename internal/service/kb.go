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
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/entity"
	"multirag/internal/utility"
	"os"
	"strings"
)

// KnowledgebaseService service class for managing dataset operations
type KnowledgebaseService struct {
	kbDAO         *dao.KnowledgebaseDAO
	userTenantDAO *dao.UserTenantDAO
	userDAO       *dao.UserDAO
	tenantDAO     *dao.TenantDAO
	connectorDAO  *dao.ConnectorDAO
	docEngine     engine.DocEngine
}

// NewKnowledgebaseService creates a new knowledge base service
func NewKnowledgebaseService(documentEngines ...engine.DocEngine) *KnowledgebaseService {
	store := engine.Get()
	if len(documentEngines) > 0 {
		store = documentEngines[0]
	}
	return &KnowledgebaseService{
		kbDAO:         dao.NewKnowledgebaseDAO(),
		userTenantDAO: dao.NewUserTenantDAO(),
		userDAO:       dao.NewUserDAO(),
		tenantDAO:     dao.NewTenantDAO(),
		connectorDAO:  dao.NewConnectorDAO(),
		docEngine:     store,
	}
}

// UpdateKBRequest represents the request for updating a knowledge base
type UpdateKBRequest struct {
	KBID         string                 `json:"kb_id" binding:"required"`
	Name         string                 `json:"name" binding:"required"`
	Description  *string                `json:"description"`
	ParserID     string                 `json:"parser_id" binding:"required"`
	Permission   *string                `json:"permission,omitempty"`
	Language     *string                `json:"language,omitempty"`
	Avatar       *string                `json:"avatar,omitempty"`
	Pagerank     *int64                 `json:"pagerank,omitempty"`
	ParserConfig map[string]interface{} `json:"parser_config,omitempty"`
	Connectors   []string               `json:"connectors,omitempty"`
}

// UpdateMetadataSettingRequest represents the request for updating metadata settings
type UpdateMetadataSettingRequest struct {
	KBID           string                 `json:"kb_id" binding:"required"`
	Metadata       map[string]interface{} `json:"metadata" binding:"required"`
	EnableMetadata *bool                  `json:"enable_metadata,omitempty"`
}

// ListKbsResponse represents the response for listing knowledge bases
type ListKbsResponse struct {
	KBs   []map[string]interface{} `json:"kbs"`
	Total int64                    `json:"total"`
}

// CreateChunkStoreRequest represents the request for creating a dataset table
type CreateChunkStoreRequest struct {
	KBID       string `json:"kb_id" binding:"required"`
	VectorSize int    `json:"vector_size" binding:"required"`
	ParserID   string `json:"parser_id,omitempty"`
}

// CreateChunkStoreResponse represents the response for creating a dataset table
type CreateChunkStoreResponse struct {
	KBID       string `json:"kb_id"`
	TableName  string `json:"table_name"`
	VectorSize int    `json:"vector_size"`
}

// CreateChunkStore creates a table in the document engine for a knowledge base
func (s *KnowledgebaseService) CreateChunkStore(ctx context.Context, req *CreateChunkStoreRequest) (*CreateChunkStoreResponse, common.ErrorCode, error) {
	if req == nil {
		return nil, common.CodeDataError, fmt.Errorf("request is required")
	}
	// Get KB to find tenant_id for building table name
	kb, err := s.kbDAO.GetByIDContext(ctx, req.KBID)
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, common.CodeDataError, fmt.Errorf("knowledge base not found: %s", req.KBID)
		}
		return nil, common.CodeServerError, fmt.Errorf("query knowledge base: %w", err)
	}

	// vector_size is required
	vecSize := req.VectorSize
	if vecSize <= 0 {
		return nil, common.CodeDataError, fmt.Errorf("vector_size must be positive")
	}

	// Build table name prefix: multirag_<tenant_id>
	tableName := fmt.Sprintf("multirag_%s", kb.TenantID)

	// Call document engine to create table
	// Full table name will be built as "{tableName}_{kb_id}"
	err = s.docEngine.CreateChunkStore(ctx, tableName, req.KBID, vecSize, req.ParserID)
	if err != nil {
		return nil, common.CodeServerError, fmt.Errorf("failed to create dataset: %w", err)
	}

	return &CreateChunkStoreResponse{
		KBID:       req.KBID,
		TableName:  tableName,
		VectorSize: vecSize,
	}, common.CodeSuccess, nil
}

// DeleteChunkStore deletes the table in the document engine for a knowledge base
func (s *KnowledgebaseService) DeleteChunkStore(ctx context.Context, kbID string) (common.ErrorCode, error) {
	// Get KB to find tenant_id for building table name
	kb, err := s.kbDAO.GetByIDContext(ctx, kbID)
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return common.CodeDataError, fmt.Errorf("knowledge base not found: %s", kbID)
		}
		return common.CodeServerError, fmt.Errorf("query knowledge base: %w", err)
	}

	// Build table name: multirag_<tenant_id>_<kb_id>
	tableName := fmt.Sprintf("multirag_%s", kb.TenantID)

	// Call document engine to delete table
	err = s.docEngine.DropChunkStore(ctx, tableName, kbID)
	if err != nil {
		return common.CodeServerError, fmt.Errorf("failed to delete table: %w", err)
	}

	return common.CodeSuccess, nil
}

// InsertChunksFromFile reads a JSON file and inserts the chunks into the dataset table.
// The file format is: {"table_name": ..., "knowledgebase_id": ..., "chunks": [...]}
func (s *KnowledgebaseService) InsertChunksFromFile(ctx context.Context, userID, filePath string) (interface{}, common.ErrorCode, error) {
	data, err := os.ReadFile(filePath)
	if err != nil {
		return nil, common.CodeDataError, fmt.Errorf("failed to read file: %w", err)
	}

	var input struct {
		IndexName       string                   `json:"index_name"`
		TableNamePrefix string                   `json:"table_name"`
		KnowledgebaseID string                   `json:"knowledgebase_id"`
		Chunks          []map[string]interface{} `json:"chunks"`
	}
	if err := json.Unmarshal(data, &input); err != nil || input.Chunks == nil {
		return nil, common.CodeDataError, fmt.Errorf(`invalid JSON format: expected {"table_name": ..., "knowledgebase_id": ..., "chunks": [...]}`)
	}
	if len(input.Chunks) == 0 {
		return nil, common.CodeDataError, fmt.Errorf("no chunks found in file")
	}

	kb, err := s.kbDAO.GetReadableByID(ctx, input.KnowledgebaseID, userID)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, common.CodeForbidden, ErrDatasetAccessDenied
	}
	if err != nil {
		return nil, common.CodeServerError, err
	}
	expected := "multirag_" + kb.TenantID
	for _, supplied := range []string{input.IndexName, input.TableNamePrefix} {
		if supplied != "" && supplied != expected {
			return nil, common.CodeDataError, fmt.Errorf("chunk namespace must match the authorized tenant")
		}
	}
	for _, chunk := range input.Chunks {
		docID, ok := chunk["doc_id"].(string)
		if !ok || strings.TrimSpace(docID) == "" || chunk["kb_id"] != kb.ID {
			return nil, common.CodeDataError, fmt.Errorf("chunk document and dataset identities are required")
		}
		var count int64
		if err := dao.DB.WithContext(ctx).Model(&entity.Document{}).Where("id = ? AND kb_id = ? AND status = ?", docID, kb.ID, string(entity.StatusValid)).Count(&count).Error; err != nil {
			return nil, common.CodeServerError, err
		}
		if count != 1 {
			return nil, common.CodeDataError, fmt.Errorf("chunk document does not belong to dataset")
		}
	}
	result, err := s.docEngine.InsertChunks(ctx, input.Chunks, expected, input.KnowledgebaseID)
	if err != nil {
		return nil, common.CodeServerError, fmt.Errorf("failed to insert into dataset: %w", err)
	}

	return result, common.CodeSuccess, nil
}

// UpdateKB updates an existing knowledge base
// This matches the Python update endpoint in kb_app.py
func (s *KnowledgebaseService) UpdateKB(req *UpdateKBRequest, userID string) (map[string]interface{}, common.ErrorCode, error) {
	// Validate name is a string
	if !isValidString(req.Name) {
		return nil, common.CodeDataError, errors.New("Dataset name must be string.")
	}

	// Trim and validate name
	name := strings.TrimSpace(req.Name)
	if name == "" {
		return nil, common.CodeDataError, errors.New("Dataset name can't be empty.")
	}

	// Check name length
	if len(name) > entity.DatasetNameLimit {
		return nil, common.CodeDataError, fmt.Errorf("Dataset name length is %d which is large than %d", len(name), entity.DatasetNameLimit)
	}

	// Check authorization
	if !s.kbDAO.Accessible4Deletion(req.KBID, userID) {
		return nil, common.CodeAuthenticationError, errors.New("No authorization.")
	}

	// Verify ownership
	kbs, err := s.kbDAO.Query(map[string]interface{}{"created_by": userID, "id": req.KBID})
	if err != nil || len(kbs) == 0 {
		return nil, common.CodeOperatingError, errors.New("only owner of dataset authorized for this operation")
	}

	// Get existing KB
	kb, err := s.kbDAO.GetByID(req.KBID)
	if err != nil {
		return nil, common.CodeDataError, errors.New("can't find this dataset")
	}

	// Check for duplicate name
	if strings.ToLower(name) != strings.ToLower(kb.Name) {
		existingKB, _ := s.kbDAO.GetByName(name, userID)
		if existingKB != nil {
			return nil, common.CodeDataError, errors.New("duplicated dataset name")
		}
	}

	// Build updates
	updates := map[string]interface{}{
		"name":      name,
		"parser_id": req.ParserID,
	}

	if req.Description != nil {
		updates["description"] = *req.Description
	}
	if req.Permission != nil {
		updates["permission"] = *req.Permission
	}
	if req.Language != nil {
		updates["language"] = *req.Language
	}
	if req.Avatar != nil {
		updates["avatar"] = *req.Avatar
	}
	if req.Pagerank != nil {
		updates["pagerank"] = *req.Pagerank
	}
	if req.ParserConfig != nil {
		updates["parser_config"] = req.ParserConfig
	}

	// Update in database
	if err := s.kbDAO.UpdateByID(req.KBID, updates); err != nil {
		return nil, common.CodeServerError, fmt.Errorf("failed to update knowledge base: %w", err)
	}

	// Get updated KB
	updatedKB, err := s.kbDAO.GetByID(req.KBID)
	if err != nil {
		return nil, common.CodeDataError, errors.New("database error (knowledgebase rename)")
	}

	result := updatedKB.ToMap()
	result["connectors"] = req.Connectors

	return result, common.CodeSuccess, nil
}

// UpdateMetadataSetting updates the metadata settings for a knowledge base
func (s *KnowledgebaseService) UpdateMetadataSetting(ctx context.Context, req *UpdateMetadataSettingRequest, userID string) (map[string]interface{}, common.ErrorCode, error) {
	if userID == "" {
		return nil, common.CodeAuthenticationError, errors.New("No authorization.")
	}
	if req == nil || strings.TrimSpace(req.KBID) == "" || req.Metadata == nil {
		return nil, common.CodeArgumentError, errors.New("kb_id and metadata are required")
	}
	var kb entity.Knowledgebase
	code := common.CodeServerError
	err := dao.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		// Lock membership before the dataset to serialize revocation with a write.
		var membership entity.UserTenant
		if err := tx.Clauses(clause.Locking{Strength: "SHARE"}).Where("user_id = ? AND status = ? AND role IN ? AND tenant_id = (SELECT tenant_id FROM t_ai_knowledgebases WHERE id = ?)", userID, "1", []string{"owner", "admin", "normal"}, req.KBID).Take(&membership).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				code = common.CodeAuthenticationError
				return errors.New("No authorization.")
			}
			return err
		}
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ? AND status = ? AND (created_by = ? OR permission = ?)", req.KBID, string(entity.StatusValid), userID, string(entity.TenantPermissionTeam)).Take(&kb).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				code = common.CodeAuthenticationError
				return errors.New("No authorization.")
			}
			return err
		}
		config := kb.ParserConfig
		if config == nil {
			config = entity.JSONMap{}
		}
		config["metadata"] = req.Metadata
		enabled := true
		if req.EnableMetadata != nil {
			enabled = *req.EnableMetadata
		}
		config["enable_metadata"] = enabled
		if err := tx.Model(&kb).Update("parser_config", config).Error; err != nil {
			return err
		}
		kb.ParserConfig = config
		return nil
	})
	if err != nil {
		if code == common.CodeAuthenticationError {
			return nil, code, errors.New("No authorization.")
		}
		return nil, code, errors.New("Unable to update metadata setting.")
	}
	return kb.ToMap(), common.CodeSuccess, nil
}

// GetDetail retrieves detailed information about a knowledge base
// This matches the Python kb_detail endpoint in kb_app.py
func (s *KnowledgebaseService) GetDetail(kbID, userID string) (*entity.KnowledgebaseDetail, common.ErrorCode, error) {
	// Check authorization
	if !s.kbDAO.Accessible(kbID, userID) {
		return nil, common.CodeOperatingError, errors.New("only owner of dataset authorized for this operation")
	}

	// Get detail
	detail, err := s.kbDAO.GetDetail(kbID)
	if err != nil {
		return nil, common.CodeDataError, errors.New("can't find this dataset")
	}

	// Set connectors (empty for now)
	detail.Connectors = []string{}

	return detail, common.CodeSuccess, nil
}

// Accessible checks if a knowledge base is accessible by a user
func (s *KnowledgebaseService) Accessible(kbID, userID string) bool {
	return s.kbDAO.Accessible(kbID, userID)
}

// RemoveTag removes a tag from documents in a dataset
func (s *KnowledgebaseService) RemoveTag(condition map[string]interface{}, newValue map[string]interface{}, indexName, kbID string) error {
	return s.docEngine.UpdateChunks(context.Background(), condition, newValue, indexName, kbID)
}

// GetByID retrieves a knowledge base by ID
func (s *KnowledgebaseService) GetByID(kbID string) (*entity.Knowledgebase, error) {
	return s.kbDAO.GetByID(kbID)
}

// GetKBIDsByTenantID retrieves all knowledge base IDs for a tenant
func (s *KnowledgebaseService) GetKBIDsByTenantID(tenantID string) ([]string, error) {
	return s.kbDAO.GetKBIDsByTenantID(tenantID)
}

// isValidString checks if a value is a non-empty string
func isValidString(v interface{}) bool {
	str, ok := v.(string)
	return ok && str != ""
}

// getParserConfig returns the parser configuration with defaults
// This matches the Python get_parser_config function
func getParserConfig(parserID string, customConfig map[string]interface{}) map[string]interface{} {
	config := map[string]interface{}{
		"pages":              [][]int{{1, 1000000}},
		"table_context_size": 0,
		"image_context_size": 0,
	}

	switch parserID {
	case "table":
		config["layout_recognize"] = false
		config["chunk_token_num"] = 128
		config["delimiter"] = "\n!?;。；！？"
		config["html4excel"] = false
	case "naive":
		config["chunk_token_num"] = 128
		config["delimiter"] = "\n!?;。；！？"
		config["html4excel"] = false
	default:
		config["raptor"] = map[string]interface{}{
			"use_raptor": false,
		}
	}

	// Merge custom config if provided
	if customConfig != nil {
		config = mergeParserConfig(config, customConfig)
	}

	return config
}

// mergeParserConfig merges two parser configurations
func mergeParserConfig(base, override map[string]interface{}) map[string]interface{} {
	result := make(map[string]interface{})
	for k, v := range base {
		result[k] = v
	}

	for k, v := range override {
		if existing, ok := result[k]; ok {
			if existingMap, ok := existing.(map[string]interface{}); ok {
				if newMap, ok := v.(map[string]interface{}); ok {
					result[k] = mergeParserConfig(existingMap, newMap)
					continue
				}
			}
		}
		result[k] = v
	}

	return result
}

// GetUserByToken gets user by authorization token
func (s *KnowledgebaseService) GetUserByToken(authorization string) (*entity.User, common.ErrorCode, error) {
	userService := NewUserService()
	return userService.GetUserByToken(authorization)
}

// GetUserByID gets user by ID
func (s *KnowledgebaseService) GetUserByID(id string) (*entity.User, error) {
	return s.userDAO.GetByAccessToken(id)
}

// GetTenantIDsByUserID gets tenant IDs for a user
func (s *KnowledgebaseService) GetTenantIDsByUserID(userID string) ([]string, error) {
	return s.userTenantDAO.GetTenantIDsByUserID(userID)
}

// GetConnectorsByTenantID gets connectors for a tenant
func (s *KnowledgebaseService) GetConnectorsByTenantID(tenantID string) ([]*dao.ConnectorListItem, error) {
	return s.connectorDAO.ListByTenantID(tenantID)
}

// GetKBList retrieves knowledge bases with ID and name filtering
func (s *KnowledgebaseService) GetKBList(tenantIDs []string, userID string, page, pageSize int, orderby string, desc bool, id, name string) ([]*entity.Knowledgebase, int64, common.ErrorCode, error) {
	kbs, total, err := s.kbDAO.GetList(tenantIDs, userID, page, pageSize, orderby, desc, id, name)
	if err != nil {
		return nil, 0, common.CodeServerError, err
	}
	return kbs, total, common.CodeSuccess, nil
}

// GetKBByIDAndUserID retrieves a knowledge base by ID and user ID
func (s *KnowledgebaseService) GetKBByIDAndUserID(kbID, userID string) ([]*entity.Knowledgebase, error) {
	return s.kbDAO.GetKBByIDAndUserID(kbID, userID)
}

// GetKBByNameAndUserID retrieves a knowledge base by name and user ID
func (s *KnowledgebaseService) GetKBByNameAndUserID(kbName, userID string) ([]*entity.Knowledgebase, error) {
	return s.kbDAO.GetKBByNameAndUserID(kbName, userID)
}

// AtomicIncreaseDocNumByID atomically increments the document count
func (s *KnowledgebaseService) AtomicIncreaseDocNumByID(kbID string) error {
	return s.kbDAO.AtomicIncreaseDocNumByID(kbID)
}

// DecreaseDocumentNum decreases document, chunk, and token counts
func (s *KnowledgebaseService) DecreaseDocumentNum(kbID string, docNum, chunkNum, tokenNum int64) error {
	return s.kbDAO.DecreaseDocumentNum(kbID, docNum, chunkNum, tokenNum)
}

// UpdateParserConfig updates the parser configuration
func (s *KnowledgebaseService) UpdateParserConfig(id string, config map[string]interface{}) error {
	return s.kbDAO.UpdateParserConfig(id, config)
}

// DeleteFieldMap removes the field_map from parser_config
func (s *KnowledgebaseService) DeleteFieldMap(id string) error {
	return s.kbDAO.DeleteFieldMap(id)
}

// GetFieldMap retrieves field mappings from multiple knowledge bases
func (s *KnowledgebaseService) GetFieldMap(ids []string) (map[string]interface{}, error) {
	return s.kbDAO.GetFieldMap(ids)
}

// GetAllIDs retrieves all knowledge base IDs
func (s *KnowledgebaseService) GetAllIDs() ([]string, error) {
	return s.kbDAO.GetAllIDs()
}

// ExtractAccessToken extracts access token from authorization header
func ExtractAccessToken(authorization, secretKey string) (string, error) {
	return utility.ExtractAccessToken(authorization, secretKey)
}
