package service

import (
	"context"
	"fmt"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"strings"
)

func (m *ModelProviderService) userInferenceModel(provider, instance, name, user string, typ entity.ModelType, api *models.APIConfig) (models.ModelDriver, *models.APIConfig, common.ErrorCode, error) {
	if api != nil && api.ModelID != nil {
		if provider != "" || instance != "" || name != "" || *api.ModelID <= 0 {
			return nil, nil, common.CodeBadRequest, fmt.Errorf("select a positive model_id or provider, instance and name exclusively")
		}
		ctx := mediaContext(api)
		tenantID, err := activeOwnedModelTenant(ctx, user)
		if err != nil {
			return nil, nil, common.CodeNotFound, err
		}
		driver, selected, resolved, err := m.getModelConfigByID(ctx, tenantID, *api.ModelID, typ, nil)
		if err != nil {
			return nil, nil, common.CodeNotFound, err
		}
		resolved.ModelName = selected
		return driver, resolved, common.CodeSuccess, nil
	}
	for _, part := range []string{provider, instance, name} {
		if strings.TrimSpace(part) == "" || strings.Contains(part, "@") {
			return nil, nil, common.CodeBadRequest, fmt.Errorf("invalid model identifier")
		}
	}
	tenants, err := m.userTenantDAO.GetByUserIDAndRole(user, "owner")
	if err != nil {
		return nil, nil, common.CodeServerError, err
	}
	if len(tenants) == 0 {
		return nil, nil, common.CodeNotFound, fmt.Errorf("user has no tenants")
	}
	driver, _, resolved, err := m.getModelConfigWithContext(mediaContext(api), tenants[0].TenantID, name+"@"+instance+"@"+provider, typ)
	if err != nil {
		return nil, nil, common.CodeDataError, err
	}
	if api != nil {
		resolved.Context = api.Context
	}
	return driver, resolved, common.CodeSuccess, nil
}

func (m *ModelProviderService) EmbedText(provider, instance, name, user string, texts []string, api *models.APIConfig, config *models.EmbeddingConfig) ([]models.EmbeddingData, common.ErrorCode, error) {
	if len(texts) == 0 || config != nil && config.Dimension < 0 {
		return nil, common.CodeBadRequest, fmt.Errorf("texts and non-negative dimension required")
	}
	driver, resolved, code, err := m.userInferenceModel(provider, instance, name, user, entity.ModelTypeEmbedding, api)
	if err != nil {
		return nil, code, err
	}
	if resolved.ModelName != "" {
		name = resolved.ModelName
	}
	bound := m.newEmbeddingModel(driver, name, resolved)
	if err := bound.ValidateDimension(config); err != nil {
		return nil, common.CodeBadRequest, err
	}
	bound.EmbeddingConfig = config
	response, err := bound.Embed(texts)
	if err != nil {
		return nil, common.CodeServerError, err
	}
	return response, common.CodeSuccess, nil
}

func (m *ModelProviderService) RerankDocument(provider, instance, name, user, query string, documents []string, api *models.APIConfig, config *models.RerankConfig) (*models.RerankResponse, common.ErrorCode, error) {
	if len(documents) == 0 || strings.TrimSpace(query) == "" {
		return nil, common.CodeBadRequest, fmt.Errorf("query and documents required")
	}
	if _, err := models.RerankTopN(len(documents), config); err != nil {
		return nil, common.CodeBadRequest, err
	}
	driver, resolved, code, err := m.userInferenceModel(provider, instance, name, user, entity.ModelTypeRerank, api)
	if err != nil {
		return nil, code, err
	}
	if resolved.ModelName != "" {
		name = resolved.ModelName
	}
	response, err := driver.Rerank(&name, query, documents, resolved, config)
	if err != nil {
		return nil, common.CodeServerError, err
	}
	if err := models.ValidateRerankResponse(response, len(documents), config); err != nil {
		return nil, common.CodeServerError, err
	}
	return response, common.CodeSuccess, nil
}

// activeOwnedModelTenant authorizes the user before any driver or provider IO.
func activeOwnedModelTenant(ctx context.Context, userID string) (string, error) {
	if ctx == nil {
		return "", fmt.Errorf("request context is required")
	}
	var owner entity.UserTenant
	if err := dao.DB.WithContext(ctx).Where("user_id = ? AND role = ? AND status = ?", userID, "owner", "1").First(&owner).Error; err != nil {
		return "", err
	}
	if err := requireActiveModelTenant(ctx, owner.TenantID); err != nil {
		return "", err
	}
	return owner.TenantID, nil
}

func modelIDFromConfig(api *models.APIConfig) *int64 {
	if api == nil {
		return nil
	}
	return api.ModelID
}

// ValidateUserInferenceSelection checks authorization and driver binding before
// opening a streaming response. It performs no remote inference.
func (m *ModelProviderService) ValidateUserInferenceSelection(provider, instance, name, user string, typ entity.ModelType, api *models.APIConfig) (common.ErrorCode, error) {
	_, _, code, err := m.userInferenceModel(provider, instance, name, user, typ, api)
	return code, err
}
