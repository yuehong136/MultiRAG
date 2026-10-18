package service

import (
	"fmt"
	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"strings"
)

func (m *ModelProviderService) userInferenceModel(provider, instance, name, user string, typ entity.ModelType, api *models.APIConfig) (models.ModelDriver, *models.APIConfig, common.ErrorCode, error) {
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
	driver, _, resolved, err := m.getModelConfig(tenants[0].TenantID, name+"@"+instance+"@"+provider, typ)
	if err != nil {
		return nil, nil, common.CodeDataError, err
	}
	if api != nil {
		resolved.Context = api.Context
	}
	return driver, resolved, common.CodeSuccess, nil
}

func (m *ModelProviderService) EmbedText(provider, instance, name, user string, texts []string, api *models.APIConfig, config *models.EmbeddingConfig) (*models.EmbeddingResponse, common.ErrorCode, error) {
	if len(texts) == 0 || config != nil && config.Dimension < 0 {
		return nil, common.CodeBadRequest, fmt.Errorf("texts and non-negative dimension required")
	}
	driver, resolved, code, err := m.userInferenceModel(provider, instance, name, user, entity.ModelTypeEmbedding, api)
	if err != nil {
		return nil, code, err
	}
	vectors, err := driver.Encode(&name, texts, resolved, config)
	if err != nil {
		return nil, common.CodeServerError, err
	}
	if err := models.ValidateEmbeddings(vectors, len(texts)); err != nil {
		return nil, common.CodeServerError, err
	}
	response := &models.EmbeddingResponse{Data: make([]models.EmbeddingResult, len(vectors))}
	for i, vector := range vectors {
		if config != nil && config.Dimension > 0 && len(vector) != config.Dimension {
			return nil, common.CodeServerError, fmt.Errorf("embedding dimension does not match request")
		}
		response.Data[i] = models.EmbeddingResult{Index: i, Dimension: len(vector)}
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
	response, err := driver.Rerank(&name, query, documents, resolved, config)
	if err != nil {
		return nil, common.CodeServerError, err
	}
	if err := models.ValidateRerankResponse(response, len(documents), config); err != nil {
		return nil, common.CodeServerError, err
	}
	return response, common.CodeSuccess, nil
}
