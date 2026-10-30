package handler

import (
	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/service"
)

func (h *DatasetsHandler) UpdateDocumentMetadataConfig(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	var req service.DocumentMetadataConfigRequest
	if err := bindExactObjectJSON(c, &req, "metadata"); err != nil {
		jsonError(c, common.CodeArgumentError, "Invalid document metadata configuration.")
		return
	}
	result, code, err := h.datasetsService.UpdateDocumentMetadataConfig(c.Request.Context(), user.ID, c.Param("dataset_id"), c.Param("document_id"), req)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, result, "success")
}
