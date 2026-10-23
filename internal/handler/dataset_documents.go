package handler

import (
	"multirag/internal/common"
	"strconv"

	"github.com/gin-gonic/gin"
)

func (h *DocumentHandler) ListDatasetDocuments(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	page, err := strconv.Atoi(c.DefaultQuery("page", "1"))
	if err != nil {
		jsonError(c, common.CodeBadRequest, "invalid page")
		return
	}
	pageSize, err := strconv.Atoi(c.DefaultQuery("page_size", "10"))
	if err != nil {
		jsonError(c, common.CodeBadRequest, "invalid page_size")
		return
	}
	emptyMetadata, err := strconv.ParseBool(c.DefaultQuery("return_empty_metadata", "true"))
	if err != nil {
		jsonError(c, common.CodeBadRequest, "invalid return_empty_metadata")
		return
	}
	result, code, err := h.documentService.ListDatasetDocuments(c.Request.Context(), c.Param("dataset_id"), user.ID, page, pageSize, c.Query("keywords"), emptyMetadata)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, result, "success")
}
