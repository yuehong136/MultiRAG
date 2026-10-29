package service

import (
	"context"
	"errors"
	"os"
	"strings"
	"unicode"

	"gorm.io/gorm"
	"multirag/internal/entity"
)

// ResolveAPITokenTenant preserves an unambiguous API key's active tenant.
// It deliberately does not fall back to web JWT, beta key or owner user identity.
func ResolveAPITokenTenant(ctx context.Context, db *gorm.DB, authorization []string) (string, error) {
	denied := errors.New("invalid API key")
	if os.Getenv("DISABLE_SDK") != "" || len(authorization) != 1 || db == nil {
		return "", denied
	}
	scheme, key, found := strings.Cut(authorization[0], " ")
	if !found || !strings.EqualFold(scheme, "Bearer") || key == "" || strings.Contains(key, ",") || strings.ContainsFunc(key, func(r rune) bool { return unicode.IsSpace(r) || unicode.IsControl(r) }) {
		return "", denied
	}
	var tokens []entity.APIToken
	err := db.WithContext(ctx).Model(&entity.APIToken{}).Select("t_ai_api_tokens.*").
		Where("t_ai_api_tokens.token=?", key).Limit(2).Find(&tokens).Error
	if err != nil || len(tokens) != 1 || tokens[0].TenantID == "" {
		return "", denied
	}
	var count int64
	if err = db.WithContext(ctx).Model(&entity.Tenant{}).Where("id=? AND status=?", tokens[0].TenantID, "1").Count(&count).Error; err != nil || count != 1 {
		return "", denied
	}
	return tokens[0].TenantID, nil
}
