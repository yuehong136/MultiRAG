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

package handler

import (
	"encoding/json"
	"errors"
	"multirag/internal/common"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"

	"multirag/internal/service"
)

// ChunkHandler chunk handler
type ChunkHandler struct {
	chunkService *service.ChunkService
	userService  *service.UserService
}

// NewChunkHandler create chunk handler
func NewChunkHandler(chunkService *service.ChunkService, userService *service.UserService) *ChunkHandler {
	return &ChunkHandler{
		chunkService: chunkService,
		userService:  userService,
	}
}

// SearchDatasets handles POST /api/v1/datasets/search with dataset_ids.
func (h *ChunkHandler) SearchDatasets(c *gin.Context) {
	user, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}

	// Bind JSON request
	var req service.RetrievalTestRequest
	if err := bindObjectJSONNullable(c, &req, map[string]bool{"doc_ids": true, "cross_languages": true, "search_id": true, "meta_data_filter": true, "tenant_rerank_id": true, "rerank_id": true, "search_mode": true, "reference_metadata": true, "metadata_condition": true}); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": err.Error(),
		})
		return
	}

	if len(req.DatasetIDs) == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"code": common.CodeBadRequest, "message": "dataset_ids must be a non-empty array"})
		return
	}
	for _, id := range req.DatasetIDs {
		if strings.TrimSpace(id) == "" {
			c.JSON(http.StatusBadRequest, gin.H{"code": common.CodeBadRequest, "message": "dataset_ids must contain non-empty strings"})
			return
		}
	}

	// Set default values for optional parameters
	if req.Page == nil {
		defaultPage := 1
		req.Page = &defaultPage
	}
	if req.Size == nil {
		defaultSize := 30
		req.Size = &defaultSize
	}
	if req.TopK == nil {
		defaultTopK := 1024
		req.TopK = &defaultTopK
	}
	if req.UseKG == nil {
		defaultUseKG := false
		req.UseKG = &defaultUseKG
	}

	// Validate required fields
	if req.Question == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "question is required",
		})
		return
	}
	if err := service.ValidateDatasetSearchRequest(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"code": common.CodeBadRequest, "message": err.Error()})
		return
	}
	// Call service with user ID for permission checks
	resp, err := h.chunkService.RetrievalTest(c.Request.Context(), &req, user.ID)
	if errors.Is(err, service.ErrRetrievalArgument) {
		c.JSON(http.StatusBadRequest, gin.H{"code": common.CodeBadRequest, "message": err.Error()})
		return
	}
	if errors.Is(err, service.ErrDatasetAccessDenied) || errors.Is(err, service.ErrSearchbotAccessDenied) {
		jsonError(c, common.CodeForbidden, "No authorization for the selected resources")
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{
			"code":    500,
			"data":    nil,
			"message": "Retrieval failed",
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    0,
		"data":    resp,
		"message": "success",
	})
}

// Get handles GET /api/v1/datasets/:dataset_id/documents/:document_id/chunks/:chunk_id.
func (h *ChunkHandler) Get(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	req := &service.GetChunkRequest{DatasetID: c.Param("dataset_id"), DocumentID: c.Param("document_id"), ChunkID: c.Param("chunk_id")}
	response, err := h.chunkService.Get(c.Request.Context(), req, user.ID)
	if errors.Is(err, service.ErrInvalidChunkScope) {
		jsonError(c, common.CodeDataError, err.Error())
		return
	}
	if errors.Is(err, service.ErrDatasetAccessDenied) {
		jsonError(c, common.CodeForbidden, err.Error())
		return
	}
	if errors.Is(err, service.ErrChunkNotFound) {
		jsonError(c, common.CodeNotFound, err.Error())
		return
	}
	if err != nil {
		jsonError(c, common.CodeServerError, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, response.Chunk, "success")
}

// List lists the chunks of a document
// @Summary List Chunks
// @Description List the chunks belonging to a document
// @Tags chunks
// @Accept json
// @Produce json
// @Param request body service.ListChunksRequest true "list chunks parameters"
// @Success 200 {object} map[string]interface{}
// @Router /v1/chunk/list [post]
func (h *ChunkHandler) List(c *gin.Context) {
	user, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}

	var req service.ListChunksRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": err.Error(),
		})
		return
	}

	// Set default values for optional parameters
	if req.Page == nil {
		defaultPage := 1
		req.Page = &defaultPage
	}
	if req.Size == nil {
		defaultSize := 30
		req.Size = &defaultSize
	}

	resp, err := h.chunkService.List(&req, user.ID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{
			"code":    500,
			"message": err.Error(),
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    0,
		"data":    resp,
		"message": "success",
	})
}

// UpdateChunk updates a chunk
// @Summary Update Chunk
// @Description Update chunk fields
// @Tags chunks
// @Accept json
// @Produce json
// @Param request body service.UpdateChunkRequest true "update chunk"
// @Success 200 {object} map[string]interface{}
// @Router /v1/chunk/update [post]
func (h *ChunkHandler) UpdateChunk(c *gin.Context) {
	user, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}

	// Validate allowed update fields and get IDs from body
	var rawBody map[string]interface{}
	if err := json.NewDecoder(c.Request.Body).Decode(&rawBody); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "invalid JSON body: " + err.Error(),
		})
		return
	}

	// Get required ID fields
	datasetID, ok := rawBody["dataset_id"].(string)
	if !ok || datasetID == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "dataset_id is required",
		})
		return
	}
	chunkID, ok := rawBody["chunk_id"].(string)
	if !ok || chunkID == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "chunk_id is required",
		})
		return
	}

	// Get document_id from request
	documentID, ok := rawBody["document_id"].(string)
	if !ok || documentID == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "doc_id is required",
		})
		return
	}

	// Allowed fields for update (exclude ID fields)
	allowedFields := map[string]bool{
		"content":            true,
		"important_keywords": true,
		"questions":          true,
		"available":          true,
		"positions":          true,
		"tag_kwd":            true,
		"tag_feas":           true,
	}
	for field := range rawBody {
		if field != "dataset_id" && field != "document_id" && field != "chunk_id" && !allowedFields[field] {
			c.JSON(http.StatusBadRequest, gin.H{
				"code":    400,
				"message": "Update field '" + field + "' is not supported. Updatable fields: content, important_keywords, questions, available, positions, tag_kwd, tag_feas",
			})
			return
		}
	}

	// Build UpdateChunkRequest from rawBody
	var req service.UpdateChunkRequest
	if content, ok := rawBody["content"].(string); ok {
		req.Content = &content
	}
	if importantKwd, ok := rawBody["important_keywords"].([]interface{}); ok {
		req.ImportantKwd = make([]string, len(importantKwd))
		for i, v := range importantKwd {
			if s, ok := v.(string); ok {
				req.ImportantKwd[i] = s
			}
		}
	}
	if questions, ok := rawBody["questions"].([]interface{}); ok {
		req.Questions = make([]string, len(questions))
		for i, v := range questions {
			if s, ok := v.(string); ok {
				req.Questions[i] = s
			}
		}
	}
	if available, ok := rawBody["available"].(bool); ok {
		req.Available = &available
	}
	if positions, ok := rawBody["positions"].([]interface{}); ok {
		req.Positions = positions
	}
	if tagKwd, ok := rawBody["tag_kwd"].([]interface{}); ok {
		req.TagKwd = make([]string, len(tagKwd))
		for i, v := range tagKwd {
			if s, ok := v.(string); ok {
				req.TagKwd[i] = s
			}
		}
	}
	req.TagFeas = rawBody["tag_feas"]

	// Set path parameters
	req.DatasetID = datasetID
	req.DocumentID = documentID
	req.ChunkID = chunkID

	err := h.chunkService.UpdateChunk(c.Request.Context(), &req, user.ID)
	if err != nil {
		code := common.CodeServerError
		switch {
		case errors.Is(err, service.ErrDatasetAccessDenied):
			code = common.CodeForbidden
		case errors.Is(err, service.ErrChunkNotFound):
			code = common.CodeNotFound
		case errors.Is(err, service.ErrInvalidChunkScope):
			code = common.CodeDataError
		}
		jsonError(c, code, err.Error())
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    0,
		"message": "chunk updated successfully",
	})
}

// Remove handles chunk removal requests
// @Summary Remove Chunks
// @Description Remove chunks from a document
// @Tags chunks
// @Accept json
// @Produce json
// @Param request body service.RemoveChunksRequest true "remove chunks request"
// @Success 200 {object} map[string]interface{}
// @Router /api/v1/datasets/{dataset_id}/documents/{document_id}/chunks [delete]
func (h *ChunkHandler) RemoveChunks(c *gin.Context) {
	user, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}

	var req service.RemoveChunksRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": err.Error(),
		})
		return
	}

	if req.DocID != "" && req.DocID != c.Param("document_id") {
		jsonError(c, common.CodeDataError, "document identity mismatch")
		return
	}
	req.DocID = c.Param("document_id")
	req.DatasetID = c.Param("dataset_id")
	if req.DocID == "" || req.DatasetID == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "doc_id is required",
		})
		return
	}

	deletedCount, err := h.chunkService.RemoveChunks(c.Request.Context(), &req, user.ID)
	if err != nil {
		code := common.CodeServerError
		switch {
		case errors.Is(err, service.ErrDatasetAccessDenied):
			code = common.CodeForbidden
		case errors.Is(err, service.ErrChunkNotFound):
			code = common.CodeNotFound
		case errors.Is(err, service.ErrInvalidChunkScope):
			code = common.CodeDataError
		}
		jsonError(c, code, err.Error())
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    0,
		"data":    deletedCount,
		"message": "success",
	})
}
