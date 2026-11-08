package handler

import (
	"github.com/gin-gonic/gin"
	"multirag/internal/common"
)

func (h *ProviderHandler) ListTenantModels(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	rows, err := h.modelProviderService.ListTenantModels(c.Request.Context(), user.ID, c.Query("type"))
	if err != nil {
		jsonError(c, common.CodeNotFound, "model inventory unavailable")
		return
	}
	jsonResponse(c, common.CodeSuccess, rows, "success")
}
