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
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"multirag/internal/common"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"

	"multirag/internal/service"
)

type documentMetadataReader interface {
	ListDocumentsByKBID(context.Context, string, string, int, int) ([]*service.DocumentResponse, int64, error)
	GetDocumentMetadataByID(context.Context, string, string) (map[string]interface{}, error)
	GetMetadataSummary(context.Context, string, string, []string) (map[string]interface{}, error)
}

// DocumentHandler document handler
type DocumentHandler struct {
	documentService *service.DocumentService
	fileService     documentFileReader
	metadataService documentMetadataReader
}

// NewDocumentHandler create document handler
func NewDocumentHandler(documentService *service.DocumentService) *DocumentHandler {
	return &DocumentHandler{
		documentService: documentService,
	}
}

func (h *DocumentHandler) metadataReader() documentMetadataReader {
	if h.metadataService != nil {
		return h.metadataService
	}
	return h.documentService
}

func documentMetadataReadError(c *gin.Context, err error) {
	status, code := http.StatusInternalServerError, common.CodeExceptionError
	if errors.Is(err, service.ErrDatasetAccessDenied) {
		status, code = http.StatusForbidden, common.CodeForbidden
	} else if errors.Is(err, service.ErrChunkNotFound) {
		status, code = http.StatusNotFound, common.CodeDataError
	}
	c.JSON(status, gin.H{"code": code, "message": "Document metadata is unavailable.", "data": nil})
}

// CreateDocument create document
// @Summary Create Document
// @Description Create new document
// @Tags documents
// @Accept json
// @Produce json
// @Param request body service.CreateDocumentRequest true "document info"
// @Success 200 {object} map[string]interface{}
// @Router /api/v1/documents [post]
func (h *DocumentHandler) CreateDocument(c *gin.Context) {
	_, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}

	var req service.CreateDocumentRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"error": err.Error(),
		})
		return
	}

	document, err := h.documentService.CreateDocument(&req)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{
			"error": err.Error(),
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"message": "created successfully",
		"data":    document,
	})
}

// GetDocumentByID get document by ID
// @Summary Get Document Info
// @Description Get document details by ID
// @Tags documents
// @Accept json
// @Produce json
// @Param id path int true "document ID"
// @Success 200 {object} map[string]interface{}
// @Router /api/v1/documents/{id} [get]
func (h *DocumentHandler) GetDocumentByID(c *gin.Context) {
	_, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}

	id := c.Param("id")
	if id == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"error": "invalid document id",
		})
		return
	}

	document, err := h.documentService.GetDocumentByID(id)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{
			"error": "document not found",
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"data": document,
	})
}

// UpdateDocument update document
// @Summary Update Document
// @Description Update document info
// @Tags documents
// @Accept json
// @Produce json
// @Param id path int true "document ID"
// @Param request body service.UpdateDocumentRequest true "update info"
// @Success 200 {object} map[string]interface{}
// @Router /api/v1/documents/{id} [put]
func (h *DocumentHandler) UpdateDocument(c *gin.Context) {
	_, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}

	id := c.Param("id")
	if id == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"error": "invalid document id",
		})
		return
	}

	var req service.UpdateDocumentRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"error": err.Error(),
		})
		return
	}

	if err := h.documentService.UpdateDocument(id, &req); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{
			"error": err.Error(),
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"message": "updated successfully",
	})
}

// ListDocuments document list
// @Summary Document List
// @Description Get paginated document list
// @Tags documents
// @Accept json
// @Produce json
// @Param page query int false "page number" default(1)
// @Param page_size query int false "items per page" default(10)
// @Success 200 {object} map[string]interface{}
// @Router /api/v1/documents [get]
func (h *DocumentHandler) ListDocuments(c *gin.Context) {
	_, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}

	page, _ := strconv.Atoi(c.DefaultQuery("page", "1"))
	pageSize, _ := strconv.Atoi(c.DefaultQuery("page_size", "10"))

	if page < 1 {
		page = 1
	}
	if pageSize < 1 || pageSize > 100 {
		pageSize = 10
	}

	documents, total, err := h.documentService.ListDocuments(page, pageSize)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{
			"error": "failed to get documents",
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"data": gin.H{
			"items":     documents,
			"total":     total,
			"page":      page,
			"page_size": pageSize,
		},
	})
}

// GetDocumentsByAuthorID get documents by author ID
// @Summary Get Author Documents
// @Description Get paginated document list by author ID
// @Tags documents
// @Accept json
// @Produce json
// @Param author_id path int true "author ID"
// @Param page query int false "page number" default(1)
// @Param page_size query int false "items per page" default(10)
// @Success 200 {object} map[string]interface{}
// @Router /api/v1/authors/{author_id}/documents [get]
func (h *DocumentHandler) GetDocumentsByAuthorID(c *gin.Context) {
	_, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}

	authorIDStr := c.Param("author_id")
	authorID, err := strconv.Atoi(authorIDStr)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"error": "invalid author id",
		})
		return
	}

	page, _ := strconv.Atoi(c.DefaultQuery("page", "1"))
	pageSize, _ := strconv.Atoi(c.DefaultQuery("page_size", "10"))

	if page < 1 {
		page = 1
	}
	if pageSize < 1 || pageSize > 100 {
		pageSize = 10
	}

	documents, total, err := h.documentService.GetDocumentsByAuthorID(authorID, page, pageSize)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{
			"error": "failed to get documents",
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"data": gin.H{
			"items":     documents,
			"total":     total,
			"page":      page,
			"page_size": pageSize,
		},
	})
}

// ListDocumentsByKB lists documents of a knowledge base with their metadata
// @Summary List Documents of a Dataset
// @Description List documents (with meta_fields) belonging to a knowledge base
// @Tags documents
// @Accept json
// @Produce json
// @Security ApiKeyAuth
// @Param id query string true "Knowledge Base ID"
// @Param page query int false "page number" default(1)
// @Param page_size query int false "items per page" default(10)
// @Success 200 {object} map[string]interface{}
// @Router /v1/document/list [post]
func (h *DocumentHandler) ListDocumentsByKB(c *gin.Context) {
	user, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}
	if user == nil || user.ID == "" {
		jsonError(c, common.CodeUnauthorized, "User not found")
		return
	}

	// Accept both "id" (multirag CLI convention) and "kb_id"
	kbID := c.Query("id")
	if kbID == "" {
		kbID = c.Query("kb_id")
	}
	if kbID == "" {
		c.JSON(http.StatusOK, gin.H{
			"code":    1,
			"message": "Lack of KB ID",
			"data":    false,
		})
		return
	}

	page, _ := strconv.Atoi(c.DefaultQuery("page", "1"))
	pageSize, _ := strconv.Atoi(c.DefaultQuery("page_size", "10"))

	if page < 1 {
		page = 1
	}
	if pageSize < 1 || pageSize > 100 {
		pageSize = 10
	}

	documents, total, err := h.metadataReader().ListDocumentsByKBID(c.Request.Context(), kbID, user.ID, page, pageSize)
	if err != nil {
		documentMetadataReadError(c, err)
		return
	}

	docs := make([]map[string]interface{}, 0, len(documents))
	for _, doc := range documents {
		metaFields, err := h.metadataReader().GetDocumentMetadataByID(c.Request.Context(), doc.ID, user.ID)
		if err != nil {
			documentMetadataReadError(c, err)
			return
		}

		docs = append(docs, map[string]interface{}{
			"id":          doc.ID,
			"name":        doc.Name,
			"size":        doc.Size,
			"type":        doc.Type,
			"status":      doc.Status,
			"created_at":  doc.CreatedAt,
			"meta_fields": metaFields,
		})
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    0,
		"message": "success",
		"data": gin.H{
			"total": total,
			"docs":  docs,
		},
	})
}

// MetadataSummary handles the metadata summary request
// @Summary Document Metadata Summary
// @Description Aggregate metadata across documents of a knowledge base
// @Tags documents
// @Accept json
// @Produce json
// @Security ApiKeyAuth
// @Param request body service.GetMetadataSummaryRequest true "metadata summary parameters"
// @Success 200 {object} map[string]interface{}
// @Router /v1/document/metadata/summary [post]
func (h *DocumentHandler) MetadataSummary(c *gin.Context) {
	user, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}
	if user == nil || user.ID == "" {
		jsonError(c, common.CodeUnauthorized, "User not found")
		return
	}

	var requestBody struct {
		KBID   string   `json:"kb_id" binding:"required"`
		DocIDs []string `json:"doc_ids"`
	}

	if err := c.ShouldBindJSON(&requestBody); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    1,
			"message": "kb_id is required",
		})
		return
	}

	kbID := requestBody.KBID
	if kbID == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    1,
			"message": "kb_id is required",
		})
		return
	}

	summary, err := h.metadataReader().GetMetadataSummary(c.Request.Context(), kbID, user.ID, requestBody.DocIDs)
	if err != nil {
		documentMetadataReadError(c, err)
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    0,
		"message": "success",
		"data": gin.H{
			"summary": summary,
		},
	})
}

// SetMetaRequest represents the request for setting document metadata
type SetMetaRequest struct {
	DocID string `json:"doc_id" binding:"required"`
	Meta  string `json:"meta" binding:"required"`
}

// SetMeta handles the set metadata request for a document
// @Summary Set Document Metadata
// @Description Set (merge) metadata for a specific document
// @Tags documents
// @Accept json
// @Produce json
// @Security ApiKeyAuth
// @Param request body SetMetaRequest true "metadata info"
// @Success 200 {object} map[string]interface{}
// @Router /v1/document/set_meta [post]
func (h *DocumentHandler) SetMeta(c *gin.Context) {
	user, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}

	var req SetMetaRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    1,
			"message": err.Error(),
		})
		return
	}

	if req.DocID == "" {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    1,
			"message": "doc_id is required",
		})
		return
	}

	// Parse meta JSON string
	var meta map[string]interface{}
	if err := json.Unmarshal([]byte(req.Meta), &meta); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    1,
			"message": "Json syntax error: " + err.Error(),
		})
		return
	}

	if meta == nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    1,
			"message": "meta is required",
		})
		return
	}

	// Validate meta values - must be str, int, float, or list of those
	for k, v := range meta {
		switch val := v.(type) {
		case string, int, float64:
			// Valid
		case []interface{}:
			for _, item := range val {
				if _, ok := item.(string); !ok {
					if _, ok := item.(float64); !ok {
						c.JSON(http.StatusBadRequest, gin.H{
							"code":    1,
							"message": fmt.Sprintf("Unsupported type in list for key %s: %T", k, item),
						})
						return
					}
				}
			}
		default:
			c.JSON(http.StatusBadRequest, gin.H{
				"code":    1,
				"message": fmt.Sprintf("Unsupported type for key %s: %T", k, v),
			})
			return
		}
	}

	err := h.documentService.SetDocumentMetadata(c.Request.Context(), req.DocID, user.ID, meta)
	if err != nil {
		errMsg := err.Error()
		if errors.Is(err, service.ErrDatasetAccessDenied) {
			jsonError(c, common.CodeForbidden, "No authorization.")
			return
		}
		if errors.Is(err, service.ErrChunkNotFound) || strings.Contains(errMsg, "no such document") || strings.Contains(errMsg, "document not found") {
			c.JSON(http.StatusBadRequest, gin.H{
				"code":    1,
				"message": errMsg,
			})
		} else {
			c.JSON(http.StatusInternalServerError, gin.H{
				"code":    1,
				"message": "Failed to set metadata: " + errMsg,
			})
		}
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    0,
		"message": "success",
		"data":    true,
	})
}

// GetThumbnails reads only thumbnails belonging to readable datasets.
func (h *DocumentHandler) GetThumbnails(c *gin.Context) {
	c.Header("Cache-Control", "no-store")
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	data, err := h.documentService.GetThumbnails(c.Request.Context(), user.ID, c.QueryArray("doc_ids"))
	if err != nil {
		code := common.CodeServerError
		if errors.Is(err, service.ErrInvalidImage) {
			code = common.CodeArgumentError
		}
		jsonError(c, code, "Thumbnails could not be read.")
		return
	}
	jsonResponse(c, common.CodeSuccess, data, "success")
}

// GetDocumentImage serves registered raster bytes after active membership authorization.
func (h *DocumentHandler) GetDocumentImage(c *gin.Context) {
	c.Header("Cache-Control", "no-store")
	c.Header("X-Content-Type-Options", "nosniff")
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	data, mime, err := h.documentService.GetDocumentImage(c.Request.Context(), user.ID, strings.TrimPrefix(c.Param("image_id"), "/"))
	if err != nil {
		code := common.CodeServerError
		if errors.Is(err, service.ErrImageUnavailable) {
			code = common.CodeDataError
		}
		if errors.Is(err, service.ErrInvalidImage) {
			code = common.CodeArgumentError
		}
		jsonError(c, code, "Image could not be read.")
		return
	}
	c.Data(http.StatusOK, mime, data)
}

// MetadataSummaryByDataset uses the same actor-scoped reader as document metadata.
// Omitted or empty doc_ids means all documents, matching the Python GET API.
func (h *DocumentHandler) MetadataSummaryByDataset(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	if user == nil || user.ID == "" {
		jsonError(c, common.CodeUnauthorized, "User not found")
		return
	}
	datasetID := c.Param("dataset_id")
	if strings.TrimSpace(datasetID) == "" {
		jsonError(c, common.CodeArgumentError, "dataset_id is required")
		return
	}
	query, err := url.ParseQuery(c.Request.URL.RawQuery)
	if err != nil {
		jsonError(c, common.CodeArgumentError, "Invalid metadata query")
		return
	}
	var docIDs []string
	for key, values := range query {
		if key != "doc_ids" || len(values) != 1 {
			jsonError(c, common.CodeArgumentError, "Invalid metadata query")
			return
		}
	}
	if value := query.Get("doc_ids"); value != "" {
		docIDs = strings.Split(value, ",")
		for _, id := range docIDs {
			if strings.TrimSpace(id) == "" || strings.TrimSpace(id) != id {
				jsonError(c, common.CodeArgumentError, "doc_ids must contain non-empty document IDs")
				return
			}
		}
	}
	summary, err := h.metadataReader().GetMetadataSummary(c.Request.Context(), datasetID, user.ID, docIDs)
	if err != nil {
		documentMetadataReadError(c, err)
		return
	}
	jsonResponse(c, common.CodeSuccess, gin.H{"summary": summary}, "success")
}
