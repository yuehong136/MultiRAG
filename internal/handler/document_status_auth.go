package handler

import (
	"fmt"
	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v4"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"net/http"
	"strings"
)

// DocumentStatusAuthMiddleware accepts the current Python web JWT as well as
// existing Go login/API tokens. The JWT subject is resolved from signed email;
// no tenant/user supplied by the status request is trusted.
func (h *AuthHandler) DocumentStatusAuthMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		raw := strings.TrimSpace(strings.TrimPrefix(c.GetHeader("Authorization"), "Bearer "))
		var user *entity.User
		var err error
		webJWT := false
		if strings.Count(raw, ".") == 2 {
			claims := &jwt.RegisteredClaims{}
			token, verifyErr := jwt.ParseWithClaims(raw, claims, func(token *jwt.Token) (interface{}, error) {
				if token.Method != jwt.SigningMethodHS256 {
					return nil, fmt.Errorf("invalid signing method")
				}
				return []byte(server.GetVariables().SecretKey), nil
			})
			if verifyErr == nil && token.Valid && claims.ExpiresAt != nil && claims.Subject != "" {
				webJWT = true
				user, err = dao.NewUserDAO().GetByEmail(claims.Subject)
			} else {
				var code common.ErrorCode
				user, code, err = h.userService.GetUserByToken(c.GetHeader("Authorization"))
				_ = code
				if err != nil {
					user, code, err = h.userService.GetUserByAPIToken(c.GetHeader("Authorization"))
				}
			}
		} else {
			var code common.ErrorCode
			user, code, err = h.userService.GetUserByAPIToken(c.GetHeader("Authorization"))
			_ = code
		}

		valid := err == nil && user != nil && user.Status != nil && *user.Status == "1" && user.IsActive && user.IsAuthenticated && !user.IsAnonymous
		if valid && webJWT && user.AccessToken != nil && strings.HasPrefix(*user.AccessToken, "INVALID_") {
			valid = false
		}
		if valid {
			var memberships int64
			err = dao.DB.Table("t_ai_user_tenants AS membership").
				Joins("JOIN t_ai_tenants AS tenant ON tenant.id = membership.tenant_id").
				Where("membership.user_id = ? AND membership.tenant_id = ? AND membership.role = ? AND membership.status = ? AND tenant.status = ?", user.ID, user.ID, "owner", "1", "1").
				Count(&memberships).Error
			valid = err == nil && memberships == 1
		}
		if !valid {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"code": common.CodeAuthenticationError, "message": "Invalid access token"})
			return
		}
		if user.IsSuperuser != nil && *user.IsSuperuser {
			c.AbortWithStatusJSON(http.StatusForbidden, gin.H{"code": common.CodeForbidden, "message": "Super user shouldn't access the URL"})
			return
		}
		if !local.IsAdminAvailable() {
			c.AbortWithStatusJSON(http.StatusServiceUnavailable, gin.H{"code": common.CodeUnauthorized, "message": "Server unavailable"})
			return
		}
		c.Set("user", user)
		c.Set("user_id", user.ID)
		c.Set("email", user.Email)
		c.Next()
	}
}
