package service

import (
	"context"
	"errors"
	"strings"
	"time"

	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
)

// CheckConnectionRequest probes submitted credentials without persisting them.
type CheckConnectionRequest struct {
	APIKey  string `json:"api_key"`
	Region  string `json:"region"`
	BaseURL string `json:"base_url"`
}

func (m *ModelProviderService) CheckConnection(ctx context.Context, providerName, userID string, req CheckConnectionRequest) (common.ErrorCode, error) {
	// Resolve active ownership before making any external request.
	var count int64
	err := dao.DB.WithContext(ctx).Model(&entity.UserTenant{}).
		Joins("JOIN t_ai_tenants t ON t.id = t_ai_user_tenants.tenant_id AND t.status = ?", "1").
		Where("t_ai_user_tenants.user_id = ? AND t_ai_user_tenants.status = ? AND t_ai_user_tenants.role = ?", userID, "1", "owner").Count(&count).Error
	if err != nil {
		return common.CodeServerError, errors.New("Unable to authorize provider check.")
	}
	if userID == "" || count == 0 {
		return common.CodeForbidden, errors.New("No authorization.")
	}
	if m.providerManager == nil {
		return common.CodeServerError, errors.New("Provider catalog unavailable.")
	}
	provider := m.providerManager.FindProvider(providerName)
	if provider == nil {
		return common.CodeNotFound, errors.New("Provider not found.")
	}
	region := normalizeModelRegion(req.Region)
	// A submitted region must never silently select another region's endpoint.
	if !strings.EqualFold(provider.Name, "Bedrock") && provider.URL[region] == "" && !(region == "default" && strings.EqualFold(provider.Class, "local")) {
		return common.CodeArgumentError, errors.New("Unsupported provider region.")
	}
	if strings.EqualFold(provider.Class, "local") && strings.TrimSpace(req.BaseURL) == "" {
		return common.CodeArgumentError, errors.New("Local provider requires base_url.")
	}
	extra, err := encodeProviderInstanceExtra(region, req.BaseURL)
	if err != nil {
		return common.CodeArgumentError, errors.New("Invalid provider endpoint.")
	}
	driver, config, err := instanceModelDriver(provider, &entity.TenantModelInstance{APIKey: strings.TrimSpace(req.APIKey), Extra: extra, Status: "active"}, m.driverFactory)
	if err != nil {
		return common.CodeArgumentError, errors.New("Invalid provider credentials or endpoint.")
	}
	probeContext, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	config.Context = probeContext
	if err := driver.CheckConnection(config); err != nil {
		return common.CodeServerError, errors.New("Provider connection failed.")
	}
	return common.CodeSuccess, nil
}
