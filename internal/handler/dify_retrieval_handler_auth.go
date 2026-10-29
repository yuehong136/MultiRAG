package handler

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"unicode"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/utility"
)

const difyTokenTenantContext = "dify_token_tenant"

// DifyAuthMiddleware preserves the original tenant of an API credential.
// A user's additional memberships cannot broaden that token's authority.
func (h *AuthHandler) DifyAuthMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		headers := c.Request.Header.Values("Authorization")
		user, tokenTenant, err := resolveDifyCredential(c.Request.Context(), headers, server.GetSecretKey())
		if err != nil {
			c.Abort()
			difyError(c, http.StatusForbidden, common.CodeForbidden, "API-KEY is invalid or missing!")
			return
		}
		if user.IsSuperuser != nil && *user.IsSuperuser {
			c.Abort()
			difyError(c, http.StatusForbidden, common.CodeForbidden, "Super user shouldn't access the URL")
			return
		}
		if !local.IsAdminAvailable() {
			c.Abort()
			difyError(c, http.StatusServiceUnavailable, common.CodeUnauthorized, "Server unavailable")
			return
		}
		c.Set("user", user)
		c.Set("user_id", user.ID)
		c.Set("email", user.Email)
		if tokenTenant != "" {
			c.Set(difyTokenTenantContext, tokenTenant)
		}
		c.Next()
	}
}

func resolveDifyCredential(ctx context.Context, authorization []string, secret string) (*entity.User, string, error) {
	if err := ctx.Err(); err != nil {
		return nil, "", err
	}
	if len(authorization) != 1 {
		return nil, "", fmt.Errorf("credential unavailable")
	}
	scheme, raw, found := strings.Cut(authorization[0], " ")
	if !found || !strings.EqualFold(scheme, "Bearer") || raw == "" || strings.Contains(raw, ",") || strings.ContainsFunc(raw, func(r rune) bool { return unicode.IsSpace(r) || unicode.IsControl(r) }) {
		return nil, "", fmt.Errorf("credential unavailable")
	}
	if dao.DB == nil || raw == "" {
		return nil, "", fmt.Errorf("credential unavailable")
	}
	var user entity.User
	tokenTenant := ""
	if strings.Count(raw, ".") == 2 {
		email, err := utility.UserJWTSubject("Bearer "+raw, secret)
		if err != nil {
			return nil, "", fmt.Errorf("invalid credential")
		}
		var matches []entity.User
		if err := dao.DB.WithContext(ctx).Where("email = ?", email).Limit(2).Find(&matches).Error; err != nil || len(matches) != 1 {
			return nil, "", fmt.Errorf("credential user unavailable")
		}
		user = matches[0]
		if user.AccessToken != nil && strings.HasPrefix(*user.AccessToken, "INVALID_") {
			return nil, "", fmt.Errorf("credential session invalidated")
		}
	} else {
		var err error
		tokenTenant, err = service.ResolveAPITokenTenant(ctx, dao.DB, authorization)
		if err != nil {
			return nil, "", fmt.Errorf("credential tenant unavailable")
		}
		var owners []entity.UserTenant
		if err := dao.DB.WithContext(ctx).Where("tenant_id = ? AND role = ? AND status = ?", tokenTenant, "owner", "1").Limit(2).Find(&owners).Error; err != nil || len(owners) != 1 {
			return nil, "", fmt.Errorf("credential owner unavailable")
		}
		if err := dao.DB.WithContext(ctx).Where("id = ?", owners[0].UserID).First(&user).Error; err != nil {
			return nil, "", fmt.Errorf("credential user unavailable")
		}
	}
	if user.ID == "" || user.Status == nil || *user.Status != "1" || !user.IsActive || !user.IsAuthenticated || user.IsAnonymous {
		return nil, "", fmt.Errorf("credential user inactive")
	}
	activeTenant := tokenTenant
	if activeTenant == "" {
		activeTenant = user.ID
	}
	var memberships int64
	if err := dao.DB.WithContext(ctx).Table("t_ai_user_tenants AS membership").
		Joins("JOIN t_ai_tenants AS tenant ON tenant.id = membership.tenant_id").
		Where("membership.user_id = ? AND membership.tenant_id = ? AND membership.role = ? AND membership.status = ? AND tenant.status = ?", user.ID, activeTenant, "owner", "1", "1").
		Count(&memberships).Error; err != nil || memberships != 1 {
		return nil, "", fmt.Errorf("credential membership inactive")
	}
	if err := ctx.Err(); err != nil {
		return nil, "", err
	}
	return &user, tokenTenant, nil
}
