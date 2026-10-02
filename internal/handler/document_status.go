package handler

import (
	"bytes"
	"encoding/json"
	"errors"
	"github.com/gin-gonic/gin"
	"io"
	"multirag/internal/common"
	"multirag/internal/service"
	"net/http"
	"strings"
)

type batchDocumentStatusRequest struct {
	DocIDs []string        `json:"doc_ids"`
	Status json.RawMessage `json:"status"`
}

func parseDocumentStatus(raw json.RawMessage) (string, error) {
	switch string(raw) {
	case "0", `"0"`:
		return "0", nil
	case "1", `"1"`:
		return "1", nil
	}
	return "", errors.New("status must be 0 or 1")
}

// BatchUpdateStatus shares Python's strict body and per-document wire contract.
func (h *DocumentHandler) BatchUpdateStatus(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	decoder := json.NewDecoder(c.Request.Body)
	decoder.DisallowUnknownFields()
	var req batchDocumentStatusRequest
	err := decoder.Decode(&req)
	if err == nil {
		var extra any
		if decoder.Decode(&extra) != io.EOF {
			err = errors.New("invalid body")
		}
	}
	status, statusErr := parseDocumentStatus(bytes.TrimSpace(req.Status))
	valid := err == nil && statusErr == nil && len(req.DocIDs) > 0
	for _, id := range req.DocIDs {
		valid = valid && strings.TrimSpace(id) != ""
	}
	if !valid {
		c.JSON(http.StatusUnprocessableEntity, gin.H{"code": common.CodeArgumentError, "message": "doc_ids must be non-empty strings and status must be 0 or 1", "data": gin.H{}})
		return
	}
	result, err := h.documentService.BatchUpdateStatus(c.Request.Context(), c.Param("dataset_id"), user.ID, req.DocIDs, status)
	if err != nil {
		code := common.CodeServerError
		if errors.Is(err, service.ErrDocumentStatusForbidden) {
			code = common.CodeAuthenticationError
		}
		c.JSON(http.StatusOK, gin.H{"code": code, "message": "Dataset unavailable or no authorization.", "data": gin.H{}})
		return
	}
	code = common.CodeSuccess
	message = "success"
	for _, item := range result {
		if _, failed := item["error"]; failed {
			code = common.CodeServerError
			message = "Partial failure"
		}
	}
	c.JSON(http.StatusOK, gin.H{"code": code, "message": message, "data": result})
}
