package handler

import (
	"encoding/json"
	"errors"
	"strings"

	"multirag/internal/common"
	"multirag/internal/service"

	"github.com/gin-gonic/gin"
)

func (h *DocumentHandler) StopParseDocuments(c *gin.Context) {
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
	var ids []string
	if err == nil {
		raw, ok := fields["document_ids"]
		if !ok || json.Unmarshal(raw, &ids) != nil {
			err = service.ErrInvalidDocumentStopRequest
		}
	}
	if err != nil || len(ids) == 0 {
		jsonError(c, common.CodeBadRequest, "document_ids must be a nonempty array of document IDs")
		return
	}
	count, err := h.documentService.StopParseDocuments(c.Request.Context(), datasetID, user.ID, ids)
	if err != nil {
		code = common.CodeServerError
		if errors.Is(err, service.ErrDocumentDeleteForbidden) || errors.Is(err, service.ErrTaskForbidden) {
			code = common.CodeForbidden
		}
		if errors.Is(err, service.ErrDocumentStopInactive) {
			code = common.CodeDataError
		}
		if errors.Is(err, service.ErrInvalidDocumentStopRequest) {
			code = common.CodeBadRequest
		}
		jsonError(c, code, "Unable to accept document cancellation")
		return
	}
	jsonResponse(c, common.CodeSuccess, gin.H{"success_count": count}, "success")
}
