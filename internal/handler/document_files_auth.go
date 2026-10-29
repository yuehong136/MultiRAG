package handler

import (
	"net/http"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/service"
)

// DatasetDocumentAuthMiddleware keeps the SDK token's tenant binding intact.
func (h *AuthHandler) DatasetDocumentAuthMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		documentFileHeaders(c)
		tenantID, err := service.ResolveAPITokenTenant(c.Request.Context(), dao.DB, c.Request.Header.Values("Authorization"))
		if err != nil {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"code": common.CodeAuthenticationError, "message": "Unauthorized", "data": nil})
			return
		}
		c.Set("document_token_tenant", tenantID)
		c.Next()
	}
}
