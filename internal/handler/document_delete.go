package handler

import (
	"bytes"
	"encoding/json"
	"errors"
	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/service"
	"strings"
)

func (h *DocumentHandler) DeleteDocuments(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	datasetID := c.Param("dataset_id")
	if datasetID == "" || strings.TrimSpace(datasetID) != datasetID {
		jsonError(c, common.CodeBadRequest, "dataset_id is required")
		return
	}
	fields, err := objectRequest(c)
	if err != nil {
		jsonError(c, common.CodeBadRequest, "Invalid document deletion request")
		return
	}
	var ids []string
	raw, hasIDs := fields["ids"]
	if hasIDs {
		if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) || json.Unmarshal(raw, &ids) != nil || ids == nil {
			jsonError(c, common.CodeBadRequest, "ids must be an array")
			return
		}
	}
	deleteAll := false
	if raw, ok := fields["delete_all"]; ok {
		if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) || json.Unmarshal(raw, &deleteAll) != nil {
			jsonError(c, common.CodeBadRequest, "delete_all must be boolean")
			return
		}
	}
	if deleteAll && hasIDs || !deleteAll && !hasIDs {
		jsonError(c, common.CodeBadRequest, "Provide ids or delete_all exclusively")
		return
	}
	deleted, err := h.documentService.DeleteDocuments(c.Request.Context(), datasetID, user.ID, ids, deleteAll)
	if err != nil {
		code = common.CodeServerError
		if errors.Is(err, service.ErrDocumentDeleteForbidden) {
			code = common.CodeForbidden
		}
		if errors.Is(err, service.ErrInvalidDocumentDeleteRequest) {
			code = common.CodeBadRequest
		}
		if deleted > 0 {
			c.JSON(200, gin.H{"code": code, "message": "Documents deleted; cleanup confirmation is incomplete", "data": gin.H{"deleted": deleted}})
			return
		}
		jsonError(c, code, "Unable to delete documents; canceled documents remain available for retry")
		return
	}
	jsonResponse(c, common.CodeSuccess, gin.H{"deleted": deleted}, "success")
}
