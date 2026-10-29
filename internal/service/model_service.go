//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

package service

import (
	"context"
	"errors"
	"fmt"
	"math"
	"os"
	"strconv"
	"strings"

	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	modelModule "multirag/internal/entity/models"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

func NewModelProviderService() *ModelProviderService {
	return NewModelProviderServiceWithFactory(nil)
}

// NewModelProviderServiceWithFactory uses the same SQL binding policy with a driver factory.
// A nil factory selects the built-in drivers.
func NewModelProviderServiceWithFactory(factory modelModule.DriverFactory) *ModelProviderService {
	return NewModelProviderServiceWithCatalog(dao.GetModelProviderManager(), factory)
}

// NewModelProviderServiceWithCatalog binds a catalog and request-local driver factory.
func NewModelProviderServiceWithCatalog(catalog *entity.ProviderManager, factory modelModule.DriverFactory) *ModelProviderService {
	return &ModelProviderService{
		providerManager:      catalog,
		driverFactory:        factory,
		modelProviderDAO:     dao.NewTenantModelProviderDAO(),
		modelInstanceDAO:     dao.NewTenantModelInstanceDAO(),
		modelDAO:             dao.NewTenantModelDAO(),
		modelGroupDAO:        dao.NewTenantModelGroupDAO(),
		modelGroupMappingDAO: dao.NewTenantModelGroupMappingDAO(),
		userTenantDAO:        dao.NewUserTenantDAO(),
	}
}

type ModelProviderService struct {
	driverFactory        modelModule.DriverFactory
	providerManager      *entity.ProviderManager
	modelProviderDAO     *dao.TenantModelProviderDAO
	modelInstanceDAO     *dao.TenantModelInstanceDAO
	modelDAO             *dao.TenantModelDAO
	modelGroupDAO        *dao.TenantModelGroupDAO
	modelGroupMappingDAO *dao.TenantModelGroupMappingDAO
	userTenantDAO        *dao.UserTenantDAO
}

func normalizeModelRegion(region string) string {
	region = strings.TrimSpace(region)
	if region == "" {
		return "default"
	}
	return region
}

func encodeModelInstanceExtra(region string) (string, error) {
	return encodeProviderInstanceExtra(region, "")
}
func decodeModelInstanceRegion(extra string) (string, error) {
	fields, err := decodeProviderInstanceExtra(extra)
	if err != nil {
		return "", err
	}
	return fields.Region, nil
}

func (m *ModelProviderService) AddModelProvider(providerName, userID string) (common.ErrorCode, error) {

	_, err := dao.GetModelProviderManager().GetProviderByName(providerName)
	if err != nil {
		return common.CodeNotFound, err
	}

	tenants, err := m.userTenantDAO.GetByUserIDAndRole(userID, "owner")
	if err != nil {
		return common.CodeServerError, err
	}

	if len(tenants) == 0 {
		return common.CodeNotFound, errors.New("user has no tenants")
	}

	tenantID := tenants[0].TenantID

	providerID, err := generateUUID1Hex()
	if err != nil {
		return common.CodeServerError, errors.New("fail to get UUID")
	}

	tenantModelProvider := &entity.TenantModelProvider{
		ID:           providerID,
		ProviderName: providerName,
		TenantID:     tenantID,
	}
	err = dao.DB.Transaction(func(tx *gorm.DB) error {
		var tenant entity.Tenant
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ? AND status = ?", tenantID, "1").Take(&tenant).Error; err != nil {
			return err
		}
		var owner entity.UserTenant
		if err := tx.Where("tenant_id = ? AND user_id = ? AND role = ? AND status = ?", tenantID, userID, "owner", "1").Take(&owner).Error; err != nil {
			return err
		}
		return tx.Create(tenantModelProvider).Error
	})
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return common.CodeNotFound, errors.New("active owned tenant unavailable")
	}
	if err != nil {
		return common.CodeServerError, fmt.Errorf("fail to create model provider: %w", err)
	}
	return common.CodeSuccess, nil
}

func (m *ModelProviderService) ListProvidersOfTenant(userID string) ([]map[string]interface{}, common.ErrorCode, error) {

	tenants, err := m.userTenantDAO.GetByUserIDAndRole(userID, "owner")
	if err != nil {
		return nil, common.CodeServerError, err
	}

	if len(tenants) == 0 {
		return nil, common.CodeNotFound, errors.New("user has no tenants")
	}

	tenantID := tenants[0].TenantID

	providerNames, err := m.modelProviderDAO.ListByID(tenantID)
	if err != nil {
		return nil, common.CodeServerError, err
	}

	var result []map[string]interface{}
	for _, providerName := range providerNames {
		provider, err := dao.GetModelProviderManager().GetProviderByName(providerName)
		if err != nil {
			return nil, common.CodeServerError, err
		}
		result = append(result, provider)
	}

	return result, common.CodeSuccess, nil
}

func (m *ModelProviderService) DeleteModelProvider(providerName, userID string) (common.ErrorCode, error) {
	tenants, err := m.userTenantDAO.GetByUserIDAndRole(userID, "owner")
	if err != nil {
		return common.CodeServerError, err
	}
	if len(tenants) == 0 {
		return common.CodeNotFound, errors.New("user has no tenants")
	}
	tenantID := tenants[0].TenantID

	_, err = m.modelProviderDAO.DeleteByTenantIDAndProviderName(tenantID, providerName)
	if err != nil {
		return common.CodeServerError, err
	}

	return common.CodeSuccess, nil
}

// ListSupportedModels is the non-HTTP entry point used by local consumers.
func (m *ModelProviderService) ListSupportedModels(providerName, instanceName, userID string) ([]entity.SupportedModel, error) {
	return m.ListSupportedModelsWithContext(context.Background(), providerName, instanceName, userID)
}

// ListSupportedModelsWithContext keeps SQL ownership checks and remote discovery
// in the request's cancellation scope. No inventory result is persisted.
func (m *ModelProviderService) ListSupportedModelsWithContext(ctx context.Context, providerName, instanceName, userID string) ([]entity.SupportedModel, error) {
	if ctx == nil {
		return nil, errors.New("request context is required")
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if m.providerManager == nil {
		return nil, errors.New("model provider catalog is unavailable")
	}
	var tenants []*entity.UserTenant
	if err := dao.DB.WithContext(ctx).Where("user_id = ? AND role = ? AND status = ?", userID, "owner", "1").Find(&tenants).Error; err != nil {
		return nil, errors.New("fail to get tenant")
	}
	if len(tenants) == 0 {
		return nil, errors.New("user has no tenants")
	}
	var provider entity.TenantModelProvider
	if err := dao.DB.WithContext(ctx).Where("tenant_id = ? AND provider_name = ?", tenants[0].TenantID, providerName).First(&provider).Error; err != nil {
		return nil, err
	}
	var instance entity.TenantModelInstance
	if err := dao.DB.WithContext(ctx).Where("provider_id = ? AND instance_name = ?", provider.ID, instanceName).First(&instance).Error; err != nil {
		return nil, err
	}
	providerInfo := m.providerManager.FindProvider(providerName)
	if providerInfo == nil {
		return nil, fmt.Errorf("provider %s not found", providerName)
	}
	driver, apiConfig, err := instanceModelDriver(providerInfo, &instance, m.driverFactory)
	if err != nil {
		return nil, err
	}
	apiConfig.Context = ctx
	names, err := driver.ListModels(apiConfig)
	if err != nil {
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return m.providerManager.DescribeSupportedModels(providerName, names), nil
}

func (m *ModelProviderService) CreateProviderInstance(providerName, instanceName, apiKey, userID, region string, baseURLs ...string) (common.ErrorCode, error) {
	if strings.TrimSpace(instanceName) == "" || strings.Contains(instanceName, "@") {
		return common.CodeBadRequest, fmt.Errorf("invalid instance name")
	}
	if strings.TrimSpace(apiKey) == "" && !strings.EqualFold(providerName, "vllm") && !strings.EqualFold(providerName, "ollama") && !strings.EqualFold(providerName, "LM-Studio") && !strings.EqualFold(providerName, "LocalAI") && !strings.EqualFold(providerName, "Xinference") && !strings.EqualFold(providerName, "mineru_local") && !strings.EqualFold(providerName, "PaddleOCR_Local") && !strings.EqualFold(providerName, "ModelScope") {
		return common.CodeBadRequest, fmt.Errorf("API key is required")
	}

	// Get tenant ID from user
	tenants, err := m.userTenantDAO.GetByUserIDAndRole(userID, "owner")
	if err != nil {
		return common.CodeServerError, err
	}

	if len(tenants) == 0 {
		return common.CodeNotFound, errors.New("user has no tenants")
	}

	tenantID := tenants[0].TenantID

	// Check if provider exists
	provider, err := m.modelProviderDAO.GetByTenantIDAndProviderName(tenantID, providerName)
	if err != nil {
		return common.CodeServerError, err
	}

	instanceID, err := generateUUID1Hex()
	if err != nil {
		return common.CodeServerError, errors.New("fail to get UUID")
	}
	baseURL := ""
	if len(baseURLs) > 0 {
		baseURL = baseURLs[0]
	}
	extra, err := encodeProviderInstanceExtra(region, baseURL)
	if err != nil {
		return common.CodeBadRequest, err
	}

	tenantModelProvider := &entity.TenantModelInstance{
		ID:           instanceID,
		InstanceName: instanceName,
		ProviderID:   provider.ID,
		APIKey:       apiKey,
		Status:       "active",
		Extra:        extra,
	}
	err = createProviderInstanceRow(provider.ID, tenantModelProvider)

	if errors.Is(err, errProviderInstanceExists) {
		return common.CodeConflict, err
	}
	if err != nil {
		return common.CodeServerError, errors.New("fail to create model instance")
	}
	return common.CodeSuccess, nil
}

func (m *ModelProviderService) ListProviderInstances(providerName, userID string) ([]map[string]interface{}, common.ErrorCode, error) {

	// Get tenant ID from user
	tenants, err := m.userTenantDAO.GetByUserIDAndRole(userID, "owner")
	if err != nil {
		return nil, common.CodeServerError, err
	}

	if len(tenants) == 0 {
		return nil, common.CodeNotFound, errors.New("user has no tenants")
	}

	tenantID := tenants[0].TenantID

	// Check if provider exists
	provider, err := m.modelProviderDAO.GetByTenantIDAndProviderName(tenantID, providerName)
	if err != nil {
		return nil, common.CodeServerError, err
	}

	// Check if provider exists
	instances, err := m.modelInstanceDAO.GetAllInstancesByProviderID(provider.ID)
	if err != nil {
		return nil, common.CodeServerError, err
	}

	var result []map[string]interface{}
	for _, instance := range instances {
		region, err := decodeModelInstanceRegion(instance.Extra)
		if err != nil {
			return nil, common.CodeServerError, err
		}
		result = append(result, map[string]interface{}{
			"id":           instance.ID,
			"instanceName": instance.InstanceName,
			"providerID":   instance.ProviderID,
			"apiKey":       instance.APIKey,
			"status":       instance.Status,
			"region":       region,
			"extra":        instance.Extra,
		})
	}

	return result, common.CodeSuccess, nil
}

func (m *ModelProviderService) ShowProviderInstance(providerName, instanceName, userID string) (map[string]interface{}, common.ErrorCode, error) {

	// Get tenant ID from user
	tenants, err := m.userTenantDAO.GetByUserIDAndRole(userID, "owner")
	if err != nil {
		return nil, common.CodeServerError, err
	}

	if len(tenants) == 0 {
		return nil, common.CodeNotFound, errors.New("user has no tenants")
	}

	tenantID := tenants[0].TenantID

	// Check if provider exists
	provider, err := m.modelProviderDAO.GetByTenantIDAndProviderName(tenantID, providerName)
	if err != nil {
		return nil, common.CodeServerError, err
	}

	instance, err := m.modelInstanceDAO.GetByProviderIDAndInstanceName(provider.ID, instanceName)
	if err != nil {
		return nil, common.CodeServerError, err
	}
	region, err := decodeModelInstanceRegion(instance.Extra)
	if err != nil {
		return nil, common.CodeServerError, err
	}

	result := map[string]interface{}{
		"id":           instance.ID,
		"instanceName": instance.InstanceName,
		"providerID":   instance.ProviderID,
		"status":       instance.Status,
		"region":       region,
		"extra":        instance.Extra,
	}

	return result, common.CodeSuccess, nil
}

func (m *ModelProviderService) ShowInstanceBalance(ctx context.Context, providerName, instanceName, userID string) (map[string]interface{}, common.ErrorCode, error) {
	if err := ctx.Err(); err != nil {
		return nil, common.CodeServerError, err
	}

	// Get tenant ID from user
	tenants, err := m.userTenantDAO.GetByUserIDAndRole(userID, "owner")
	if err != nil {
		return nil, common.CodeServerError, err
	}

	if len(tenants) == 0 {
		return nil, common.CodeNotFound, errors.New("user has no tenants")
	}

	tenantID := tenants[0].TenantID

	// Check if provider exists
	provider, err := m.modelProviderDAO.GetByTenantIDAndProviderName(tenantID, providerName)
	if err != nil {
		return nil, common.CodeServerError, err
	}

	instance, err := m.modelInstanceDAO.GetByProviderIDAndInstanceName(provider.ID, instanceName)
	if err != nil {
		return nil, common.CodeServerError, err
	}

	providerInfo := dao.GetModelProviderManager().FindProvider(providerName)
	if providerInfo == nil {
		return nil, common.CodeServerError, fmt.Errorf("provider %s not found", providerName)
	}

	driver, apiConfig, err := instanceModelDriver(providerInfo, instance, m.driverFactory)
	if err != nil {
		return nil, common.CodeDataError, err
	}
	apiConfig.Context = ctx
	result, err := driver.Balance(apiConfig)
	if err != nil {
		return nil, common.CodeServerError, err
	}
	return result, common.CodeSuccess, nil
}

func (m *ModelProviderService) CheckProviderConnection(providerName, instanceName, userID string) (common.ErrorCode, error) {
	tenants, err := m.userTenantDAO.GetByUserIDAndRole(userID, "owner")
	if err != nil {
		return common.CodeServerError, err
	}
	if len(tenants) == 0 {
		return common.CodeNotFound, errors.New("user has no tenants")
	}

	provider, err := m.modelProviderDAO.GetByTenantIDAndProviderName(tenants[0].TenantID, providerName)
	if err != nil {
		return common.CodeNotFound, err
	}
	instance, err := m.modelInstanceDAO.GetByProviderIDAndInstanceName(provider.ID, instanceName)
	if err != nil {
		return common.CodeNotFound, err
	}
	providerInfo := dao.GetModelProviderManager().FindProvider(providerName)
	if providerInfo == nil {
		return common.CodeNotFound, fmt.Errorf("provider %s not found", providerName)
	}
	driver, apiConfig, err := instanceModelDriver(providerInfo, instance)
	if err != nil {
		return common.CodeDataError, err
	}
	if err = driver.CheckConnection(apiConfig); err != nil {
		return common.CodeServerError, err
	}
	return common.CodeSuccess, nil
}

func (m *ModelProviderService) AlterProviderInstance(providerName, instanceName, newInstanceName, apiKey, userID string) (common.ErrorCode, error) {
	return common.CodeBadRequest, errors.New("provider instance alteration is not implemented")
}

func (m *ModelProviderService) ListInstanceModels(providerName, instanceName, userID string) ([]map[string]interface{}, error) {
	// Get tenant ID from user
	tenants, err := m.userTenantDAO.GetByUserIDAndRole(userID, "owner")
	if err != nil {
		return nil, err
	}

	if len(tenants) == 0 {
		return nil, errors.New("user has no tenants")
	}

	tenantID := tenants[0].TenantID

	// Check if provider exists
	provider, err := m.modelProviderDAO.GetByTenantIDAndProviderName(tenantID, providerName)
	if err != nil {
		return nil, err
	}

	// Get instance
	instance, err := m.modelInstanceDAO.GetByProviderIDAndInstanceName(provider.ID, instanceName)
	if err != nil {
		return nil, err
	}

	// Get all models for this instance
	disabledModels, err := m.modelDAO.GetModelsByInstanceID(instance.ID)
	if err != nil {
		return nil, err
	}

	allModels, err := m.providerManager.ListModels(providerName)
	if err != nil {
		return nil, err
	}
	byName := make(map[string]map[string]interface{})
	for _, model := range allModels {
		model["status"] = "active"
		byName[model["name"].(string)] = model
	}
	for _, stored := range disabledModels {
		data, exists := byName[stored.ModelName]
		if !exists {
			data = map[string]interface{}{"name": stored.ModelName, "model_types": []string{stored.ModelType}}
			allModels = append(allModels, data)
		}
		data["status"] = "inactive"
		if stored.Status == "active" && stored.Extra != "" && stored.Extra != "{}" {
			data["status"] = "active"
		}
		if stored.Extra != "" && stored.Extra != "{}" {
			extra, err := decodeCustomModelExtra(stored.Extra)
			if err != nil {
				return nil, err
			}
			types, err := normalizeCustomModelTypes(stored.ModelType, extra.ModelTypes)
			if err != nil {
				return nil, err
			}
			data["model_types"] = types
			data["model_type"] = types[0]
			data["max_tokens"] = extra.MaxTokens
			data["extra"] = stored.Extra
			if extra.Thinking != nil {
				data["thinking"] = *extra.Thinking
			}
		}
	}
	return allModels, nil
}

func (m *ModelProviderService) UpdateModelStatus(providerName, instanceName, modelName, userID, status string) (common.ErrorCode, error) {
	active := status == "enable" || status == "enabled" || status == "active"
	if !active && status != "disable" && status != "disabled" && status != "inactive" {
		return common.CodeBadRequest, fmt.Errorf("invalid model status")
	}
	return m.mutateOwnedModelResources(providerName, userID, func(tx *gorm.DB, provider *entity.TenantModelProvider) error {
		var instance entity.TenantModelInstance
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("provider_id = ? AND instance_name = ?", provider.ID, instanceName).First(&instance).Error; err != nil {
			return err
		}
		var model entity.TenantModel
		err := tx.Where("provider_id = ? AND instance_id = ? AND model_name = ?", provider.ID, instance.ID, modelName).First(&model).Error
		if err != nil && !errors.Is(err, gorm.ErrRecordNotFound) {
			return err
		}
		if err == nil {
			if model.Extra != "" && model.Extra != "{}" {
				state := "inactive"
				if active {
					state = "active"
				}
				return tx.Model(&model).Update("status", state).Error
			}
			if active {
				return tx.Unscoped().Delete(&model).Error
			}
			return nil
		}
		schema, err := m.providerManager.GetModelByName(providerName, modelName)
		if err != nil {
			return gorm.ErrRecordNotFound
		}
		if active {
			return nil
		}
		id, err := generateUUID1Hex()
		if err != nil {
			return err
		}
		return tx.Create(&entity.TenantModel{ID: id, ProviderID: provider.ID, InstanceID: instance.ID, ModelName: modelName, ModelType: schema.ModelTypes[0], Status: status}).Error
	})
}

func (m *ModelProviderService) ChatToModel(providerName, instanceName, modelName, userID, message string, apiConfig *modelModule.APIConfig, modelConfig *modelModule.ChatConfig) (*modelModule.ChatResponse, common.ErrorCode, error) {
	return m.ChatToModelWithMessages(providerName, instanceName, modelName, userID, []modelModule.Message{{Role: "user", Content: message}}, apiConfig, modelConfig)
}

func (m *ModelProviderService) ChatToModelWithMessages(providerName, instanceName, modelName, userID string, messages []modelModule.Message, apiConfig *modelModule.APIConfig, modelConfig *modelModule.ChatConfig) (*modelModule.ChatResponse, common.ErrorCode, error) {
	if err := modelModule.ValidateMessages(messages); err != nil {
		return nil, common.CodeBadRequest, err
	}
	bound, code, err := m.userInstanceChatModel(providerName, instanceName, modelName, userID, apiConfig, modelConfig)
	if err != nil {
		return nil, code, err
	}
	response, err := bound.ChatWithMessages(messages, nil)
	if err != nil {
		return nil, common.CodeServerError, err
	}
	if err := modelModule.ValidateChatResponse(response); err != nil {
		return nil, common.CodeServerError, err
	}
	return response, common.CodeSuccess, nil
}

// ChatToModelStream streams chat response via a channel (better performance)
func (m *ModelProviderService) ChatToModelStream(providerName, instanceName, modelName, userID, message string) (<-chan string, <-chan error, common.ErrorCode, error) {
	bound, code, err := m.userInstanceChatModel(providerName, instanceName, modelName, userID, nil, nil)
	streamChan, errChan := make(chan string), make(chan error, 1)
	if err != nil {
		close(streamChan)
		close(errChan)
		return streamChan, errChan, code, err
	}
	go func() {
		defer close(streamChan)
		defer close(errChan)
		err := bound.ModelDriver.ChatStreamlyWithChannel(bound.ModelName, bound.APIConfig.APIKey, &message, nil, streamChan)
		if err != nil {
			errChan <- err
		}
	}()
	return streamChan, errChan, common.CodeSuccess, nil
}

// applyModelChatDefaults respects explicit false and uses the selected model's defaults.
func applyModelChatDefaults(model *entity.Model, config *modelModule.ChatConfig) {
	config.ModelClass = model.Class
	if config.Thinking == nil && model.Thinking != nil {
		value := model.Thinking.DefaultValue
		config.Thinking = &value
	}
}

// ChatToModelStreamWithSender streams chat response directly via sender function (best performance, no channel)
func (m *ModelProviderService) ChatToModelStreamWithSender(providerName, instanceName, modelName, userID, message string, apiConfig *modelModule.APIConfig, modelConfig *modelModule.ChatConfig, sender func(*string, *string) error) (common.ErrorCode, error) {
	return m.ChatToModelStreamWithMessages(providerName, instanceName, modelName, userID, []modelModule.Message{{Role: "user", Content: message}}, apiConfig, modelConfig, sender)
}

func (m *ModelProviderService) ChatToModelStreamWithMessages(providerName, instanceName, modelName, userID string, messages []modelModule.Message, apiConfig *modelModule.APIConfig, modelConfig *modelModule.ChatConfig, sender func(*string, *string) error) (common.ErrorCode, error) {
	if err := modelModule.ValidateMessages(messages); err != nil {
		return common.CodeBadRequest, err
	}
	bound, code, err := m.userInstanceChatModel(providerName, instanceName, modelName, userID, apiConfig, modelConfig)
	if err != nil {
		return code, err
	}
	if err := bound.ChatStreamlyWithMessages(messages, nil, sender); err != nil {
		return common.CodeServerError, err
	}
	return common.CodeSuccess, nil
}

// GetEmbeddingModel binds the shared driver to tenant credentials.
func (m *ModelProviderService) GetEmbeddingModel(tenantID, compositeName string) (*modelModule.EmbeddingModel, error) {
	return m.bindEmbeddingModel(context.Background(), tenantID, compositeName)
}

func (m *ModelProviderService) GetEmbeddingModelWithContext(ctx context.Context, tenantID, compositeName string) (*modelModule.EmbeddingModel, error) {
	if err := requireActiveModelTenant(ctx, tenantID); err != nil {
		return nil, err
	}
	return m.bindEmbeddingModel(ctx, tenantID, compositeName)
}

func (m *ModelProviderService) bindEmbeddingModel(ctx context.Context, tenantID, compositeName string) (*modelModule.EmbeddingModel, error) {
	driver, name, config, err := m.getModelConfigWithContext(ctx, tenantID, compositeName, entity.ModelTypeEmbedding)
	if err != nil {
		return nil, err
	}
	return modelModule.NewEmbeddingModel(driver, &name, config), nil
}
func (m *ModelProviderService) GetRerankModel(tenantID, compositeName string) (*modelModule.RerankModel, error) {
	return m.bindRerankModel(context.Background(), tenantID, compositeName)
}

func (m *ModelProviderService) GetRerankModelWithContext(ctx context.Context, tenantID, compositeName string) (*modelModule.RerankModel, error) {
	if err := requireActiveModelTenant(ctx, tenantID); err != nil {
		return nil, err
	}
	return m.bindRerankModel(ctx, tenantID, compositeName)
}

func (m *ModelProviderService) bindRerankModel(ctx context.Context, tenantID, compositeName string) (*modelModule.RerankModel, error) {
	driver, name, config, err := m.getModelConfigWithContext(ctx, tenantID, compositeName, entity.ModelTypeRerank)
	if err != nil {
		return nil, err
	}
	return modelModule.NewRerankModel(driver, &name, config), nil
}
func (m *ModelProviderService) GetChatModel(tenantID, compositeName string) (*modelModule.ChatModel, error) {
	return m.bindChatModel(context.Background(), tenantID, compositeName)
}

func (m *ModelProviderService) GetChatModelWithContext(ctx context.Context, tenantID, compositeName string) (*modelModule.ChatModel, error) {
	if err := requireActiveModelTenant(ctx, tenantID); err != nil {
		return nil, err
	}
	return m.bindChatModel(ctx, tenantID, compositeName)
}

func (m *ModelProviderService) bindChatModel(ctx context.Context, tenantID, compositeName string) (*modelModule.ChatModel, error) {
	defaults := modelModule.ChatConfig{}
	driver, name, config, err := m.getModelConfigWithContext(ctx, tenantID, compositeName, entity.ModelTypeChat, &defaults)
	if err != nil {
		return nil, err
	}
	result := modelModule.NewChatModel(driver, &name, config)
	if defaults.ModelClass == nil && m.providerManager != nil {
		providerName := modelProviderName(compositeName)
		if providerName == "" {
			providerName = driver.Name()
		}
		if definition, err := m.providerManager.GetModelByName(providerName, name); err == nil {
			applyModelChatDefaults(definition, &defaults)
		}
	}
	result.ModelConfig = defaults
	return result, nil
}

func modelProviderName(compositeName string) string {
	_, provider, _ := strings.Cut(compositeName, "@")
	if index := strings.LastIndex(provider, "@"); index >= 0 {
		provider = provider[index+1:]
	}
	return provider
}

// splitModelInstance accepts legacy model@provider and current
// model@instance@provider names written by SetTenantDefaultModels.
func splitModelInstance(compositeName string) (string, string, string, error) {
	parts := strings.Split(compositeName, "@")
	if len(parts) != 2 && len(parts) != 3 {
		return "", "", "", fmt.Errorf("invalid model name format: %s", compositeName)
	}
	for _, part := range parts {
		if strings.TrimSpace(part) == "" {
			return "", "", "", fmt.Errorf("empty model name component")
		}
	}
	instance := "default"
	if len(parts) == 3 {
		instance = parts[1]
	}
	return parts[0], instance, parts[len(parts)-1], nil
}

func (m *ModelProviderService) getModelConfig(tenantID, compositeName string, modelType entity.ModelType, chatDefaults ...*modelModule.ChatConfig) (modelModule.ModelDriver, string, *modelModule.APIConfig, error) {
	return m.getModelConfigWithContext(context.Background(), tenantID, compositeName, modelType, chatDefaults...)
}

func (m *ModelProviderService) getModelConfigWithContext(ctx context.Context, tenantID, compositeName string, modelType entity.ModelType, chatDefaults ...*modelModule.ChatConfig) (modelModule.ModelDriver, string, *modelModule.APIConfig, error) {
	return m.getModelConfigWithIdentity(ctx, tenantID, compositeName, modelType, nil, chatDefaults...)
}

func (m *ModelProviderService) getModelConfigWithIdentity(ctx context.Context, tenantID, compositeName string, modelType entity.ModelType, identity *EmbeddingModelIdentity, chatDefaults ...*modelModule.ChatConfig) (modelModule.ModelDriver, string, *modelModule.APIConfig, error) {
	if err := ctx.Err(); err != nil {
		return nil, "", nil, err
	}
	if compositeName == "" {
		var err error
		var tenant entity.Tenant
		err = dao.DB.WithContext(ctx).Where("id = ? AND status = ?", tenantID, "1").First(&tenant).Error
		if err == nil {
			if id := tenantDefaultBindingID(&tenant, modelType); id != nil {
				return m.getModelConfigByID(ctx, tenantID, *id, modelType, identity)
			}
			compositeName, err = tenantDefaultModelName(&tenant, modelType)
		}
		if err != nil {
			return nil, "", nil, err
		}
		if strings.TrimSpace(compositeName) == "" {
			return nil, "", nil, fmt.Errorf("no default %s model is set", modelType)
		}
	}
	name, instanceName, providerName, err := splitModelInstance(compositeName)
	if err != nil {
		return nil, "", nil, err
	}
	if strings.EqualFold(providerName, "Builtin") {
		return m.getBuiltinModelConfig(ctx, tenantID, name, instanceName, modelType, identity)
	}
	if m.providerManager == nil {
		return nil, "", nil, fmt.Errorf("model providers are not initialized")
	}
	providerInfo := m.providerManager.FindProvider(providerName)
	if providerInfo == nil && strings.EqualFold(providerName, "GiteeAI") {
		providerInfo = m.providerManager.FindProvider("Gitee")
	}
	if providerInfo == nil && strings.EqualFold(providerName, "OpenAI-API-Compatible") {
		providerInfo = &entity.Provider{Name: providerName, URL: map[string]string{}, URLSuffix: modelModule.URLSuffix{Embedding: "embeddings"}}
	}
	if providerInfo == nil {
		return nil, "", nil, fmt.Errorf("provider %s not found", providerName)
	}
	var provider entity.TenantModelProvider
	err = dao.DB.WithContext(ctx).Where("tenant_id = ? AND provider_name = ?", tenantID, providerName).First(&provider).Error
	if errors.Is(err, gorm.ErrRecordNotFound) && strings.Count(compositeName, "@") == 1 {
		return m.getLegacyModelConfigWithContext(ctx, tenantID, providerName, name, modelType, providerInfo, identity)
	}

	if err != nil {
		return nil, "", nil, err
	}
	var instance entity.TenantModelInstance
	err = dao.DB.WithContext(ctx).Where("provider_id = ? AND instance_name = ?", provider.ID, instanceName).First(&instance).Error
	if errors.Is(err, gorm.ErrRecordNotFound) && strings.Count(compositeName, "@") == 1 {
		return m.getLegacyModelConfigWithContext(ctx, tenantID, providerName, name, modelType, providerInfo, identity)
	}
	if err != nil {
		return nil, "", nil, err
	}
	if instance.Status != "active" {
		return nil, "", nil, errModelInstanceDisabled
	}
	model, err := m.instanceModelDefinitionWithContext(ctx, providerInfo, provider.ID, instance.ID, name)
	if err != nil {
		return nil, "", nil, err
	}
	supported := false
	for _, typ := range model.ModelTypes {
		if modelTypeMatches(typ, modelType) {
			supported = true
		}
	}
	if !supported {
		return nil, "", nil, fmt.Errorf("model type mismatch")
	}
	if len(chatDefaults) > 0 {
		applyModelChatDefaults(model, chatDefaults[0])
	}
	driver, config, err := instanceModelDriver(providerInfo, &instance, m.driverFactory)
	if err == nil && identity != nil {
		extra, extraErr := decodeProviderInstanceExtra(instance.Extra)
		if extraErr != nil {
			return nil, "", nil, extraErr
		}
		endpoint := extra.BaseURL
		if endpoint == "" {
			endpoint = providerInfo.URL[extra.Region]
		}
		*identity, err = resolvedEmbeddingIdentity(driver, providerInfo.Name, name, endpoint, extra.Region, config)
		if err != nil {
			return nil, "", nil, err
		}
	}
	if config != nil {
		config.Context = ctx
	}
	return driver, name, config, err
}

// getLegacyModelConfig keeps old tenant credentials usable while removing the
// duplicate provider implementation. Only a missing provider/default instance
// can select this path; database failures and disabled models remain errors.
func (m *ModelProviderService) getLegacyModelConfigWithContext(ctx context.Context, tenantID, providerName, name string, modelType entity.ModelType, providerInfo *entity.Provider, identities ...*EmbeddingModelIdentity) (modelModule.ModelDriver, string, *modelModule.APIConfig, error) {
	var legacy entity.TenantLLM
	err := dao.DB.WithContext(ctx).Where("tenant_id = ? AND llm_factory = ? AND llm_name = ?", tenantID, providerName, name).First(&legacy).Error
	if err != nil {
		return nil, "", nil, err
	}
	return m.bindLegacyModelConfig(ctx, name, modelType, providerInfo, &legacy, identities...)
}

// A numeric TenantLLM selector binds this exact row; it must never be re-resolved
// through a modern same-name provider instance.
func (m *ModelProviderService) bindLegacyModelConfig(ctx context.Context, name string, modelType entity.ModelType, providerInfo *entity.Provider, legacy *entity.TenantLLM, identities ...*EmbeddingModelIdentity) (modelModule.ModelDriver, string, *modelModule.APIConfig, error) {
	if legacy.Status != "1" {
		return nil, "", nil, errModelDisabled
	}
	if legacy.ModelType == nil || strings.TrimSpace(*legacy.ModelType) == "" {
		// A nullable legacy declaration can use a known catalog capability;
		// an unknown custom model has no evidence authorizing any capability.
		definition, err := m.providerManager.GetModelByName(providerInfo.Name, name)
		if err != nil {
			return nil, "", nil, fmt.Errorf("legacy model type is missing")
		}
		supported := false
		for _, typ := range definition.ModelTypes {
			supported = supported || modelTypeMatches(typ, modelType)
		}
		if !supported {
			return nil, "", nil, fmt.Errorf("model type mismatch")
		}
	} else if !modelTypeMatches(*legacy.ModelType, modelType) {
		return nil, "", nil, fmt.Errorf("model type mismatch")
	}
	if legacy.APIKey == nil || *legacy.APIKey == "" {
		return nil, "", nil, fmt.Errorf("model API key is missing")
	}
	urls := providerInfo.URL
	if legacy.APIBase != nil && *legacy.APIBase != "" {
		extra, err := decodeProviderInstanceExtra(`{"region":"default","base_url":` + strconv.Quote(*legacy.APIBase) + `}`)
		if err != nil {
			return nil, "", nil, err
		}
		urls = map[string]string{"default": extra.BaseURL}
	}
	if strings.TrimSpace(urls["default"]) == "" && !strings.EqualFold(providerInfo.Name, "Bedrock") {
		return nil, "", nil, fmt.Errorf("model base URL is missing")
	}
	driver, err := modelModule.NewModelFactory().CreateModelDriver(providerInfo.Name, urls, providerInfo.URLSuffix)
	if err == nil && len(identities) > 0 && identities[0] != nil {
		*identities[0], err = resolvedEmbeddingIdentity(driver, providerInfo.Name, name, urls["default"], "default", &modelModule.APIConfig{Context: ctx, APIKey: legacy.APIKey})
		if err != nil {
			return nil, "", nil, err
		}
	}
	return driver, name, &modelModule.APIConfig{Context: ctx, APIKey: legacy.APIKey}, err
}

// Chat accepts the multimodal capability as well as text chat. Media remains
// specific: OCR must never select a TTS or image-to-text declaration.
func modelTypeMatches(raw string, expected entity.ModelType) bool {
	actual, err := normalizeCustomModelType(raw)
	if err != nil {
		return false
	}
	return actual == expected || expected == entity.ModelTypeChat && actual == entity.ModelTypeImage2Text
}

// Builtin is an explicitly configured shared TEI service. A name alone cannot
// authorize use: the current tenant must own an active embedding declaration.
func (m *ModelProviderService) getBuiltinModelConfig(ctx context.Context, tenantID, name, instance string, modelType entity.ModelType, identity *EmbeddingModelIdentity) (modelModule.ModelDriver, string, *modelModule.APIConfig, error) {
	if modelType != entity.ModelTypeEmbedding || instance != "default" {
		return nil, "", nil, fmt.Errorf("builtin supports the default embedding instance only")
	}
	configured := false
	for _, profile := range strings.FieldsFunc(os.Getenv("COMPOSE_PROFILES"), func(r rune) bool { return r == ',' || r == ' ' || r == '\t' || r == '\n' }) {
		configured = configured || strings.HasPrefix(profile, "tei-")
	}
	endpoint := strings.TrimSpace(os.Getenv("TEI_BASE_URL"))
	if !configured || name != strings.TrimSpace(os.Getenv("TEI_MODEL")) || endpoint == "" {
		return nil, "", nil, fmt.Errorf("builtin TEI model is not configured")
	}
	var tenant entity.Tenant
	if err := dao.DB.WithContext(ctx).Where("id = ? AND status = ?", tenantID, "1").First(&tenant).Error; err != nil {
		return nil, "", nil, err
	}
	var binding entity.TenantLLM
	if err := dao.DB.WithContext(ctx).Where("tenant_id = ? AND llm_factory = ? AND llm_name = ? AND mdl_type = ? AND status = ?", tenantID, "Builtin", name, entity.ModelTypeEmbedding, "1").First(&binding).Error; err != nil {
		return nil, "", nil, err
	}
	driver, err := modelModule.NewBuiltinModel(endpoint, name)
	if err != nil {
		return nil, "", nil, err
	}
	if identity != nil {
		*identity = EmbeddingModelIdentity{Provider: "builtin", Model: name, Endpoint: strings.TrimRight(endpoint, "/"), Region: "default"}
	}
	return driver, name, &modelModule.APIConfig{Context: ctx}, nil
}

// EmbeddingModelIdentity describes the selected embedding space without tenant
// credentials. Dimensions are checked from the stored vectors by retrieval;
// the model catalog has no reliable dimension field.
type EmbeddingModelIdentity struct {
	Provider string
	Model    string
	Endpoint string
	Region   string
}

// ResolvedEmbeddingIdentity performs the same active, typed SQL binding as an
// embedding call, without sending a provider request or exposing its API key.
func (m *ModelProviderService) ResolvedEmbeddingIdentity(ctx context.Context, tenantID, compositeName string) (EmbeddingModelIdentity, error) {
	var identity EmbeddingModelIdentity
	if err := requireActiveModelTenant(ctx, tenantID); err != nil {
		return identity, err
	}
	_, _, _, err := m.getModelConfigWithIdentity(ctx, tenantID, compositeName, entity.ModelTypeEmbedding, &identity)
	return identity, err
}

func requireActiveModelTenant(ctx context.Context, tenantID string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	var tenant entity.Tenant
	return dao.DB.WithContext(ctx).Select("id").Where("id = ? AND status = ?", tenantID, "1").First(&tenant).Error
}

func resolvedEmbeddingIdentity(driver modelModule.ModelDriver, provider, name, endpoint, region string, config *modelModule.APIConfig) (EmbeddingModelIdentity, error) {
	if resolver, ok := driver.(interface {
		ResolveEmbeddingEndpoint(string, *modelModule.APIConfig) (string, string, error)
	}); ok {
		var err error
		endpoint, region, err = resolver.ResolveEmbeddingEndpoint(name, config)
		if err != nil {
			return EmbeddingModelIdentity{}, err
		}
	}
	extra, err := decodeProviderInstanceExtra(`{"region":"default","base_url":` + strconv.Quote(endpoint) + `}`)
	if err != nil {
		return EmbeddingModelIdentity{}, err
	}
	if extra.BaseURL == "" {
		return EmbeddingModelIdentity{}, fmt.Errorf("embedding identity endpoint is unavailable")
	}
	return EmbeddingModelIdentity{Provider: strings.ToLower(provider), Model: name, Endpoint: strings.TrimRight(extra.BaseURL, "/"), Region: region}, nil
}

func (m *ModelProviderService) getModelConfigByID(ctx context.Context, tenantID string, id int64, modelType entity.ModelType, identity *EmbeddingModelIdentity) (modelModule.ModelDriver, string, *modelModule.APIConfig, error) {
	if err := requireActiveModelTenant(ctx, tenantID); err != nil {
		return nil, "", nil, err
	}
	if id <= 0 {
		return nil, "", nil, fmt.Errorf("model binding must be positive")
	}
	var binding entity.TenantLLM
	if err := dao.DB.WithContext(ctx).Where("id = ? AND tenant_id = ? AND status = ?", id, tenantID, "1").First(&binding).Error; err != nil {
		return nil, "", nil, err
	}
	if binding.LLMName == nil || strings.TrimSpace(*binding.LLMName) == "" || binding.ModelType == nil || !modelTypeMatches(*binding.ModelType, modelType) {
		return nil, "", nil, fmt.Errorf("active typed model binding unavailable")
	}
	name := *binding.LLMName
	if strings.EqualFold(binding.LLMFactory, "Builtin") {
		return m.getBuiltinModelConfig(ctx, tenantID, name, "default", modelType, identity)
	}
	if m.providerManager == nil {
		return nil, "", nil, fmt.Errorf("model providers are not initialized")
	}
	provider := m.providerManager.FindProvider(binding.LLMFactory)
	if provider == nil && strings.EqualFold(binding.LLMFactory, "GiteeAI") {
		provider = m.providerManager.FindProvider("Gitee")
	}
	if provider == nil && strings.EqualFold(binding.LLMFactory, "OpenAI-API-Compatible") {
		provider = &entity.Provider{Name: binding.LLMFactory, URL: map[string]string{}, URLSuffix: modelModule.URLSuffix{Embedding: "embeddings"}}
	}
	if provider == nil {
		return nil, "", nil, fmt.Errorf("model provider unavailable")
	}
	return m.bindLegacyModelConfig(ctx, name, modelType, provider, &binding, identity)
}

func (m *ModelProviderService) GetEmbeddingModelByIDWithContext(ctx context.Context, tenantID string, id int64) (*modelModule.EmbeddingModel, error) {
	driver, name, config, err := m.getModelConfigByID(ctx, tenantID, id, entity.ModelTypeEmbedding, nil)
	if err != nil {
		return nil, err
	}
	return modelModule.NewEmbeddingModel(driver, &name, config), nil
}
func (m *ModelProviderService) GetRerankModelByIDWithContext(ctx context.Context, tenantID string, id int64) (*modelModule.RerankModel, error) {
	driver, name, config, err := m.getModelConfigByID(ctx, tenantID, id, entity.ModelTypeRerank, nil)
	if err != nil {
		return nil, err
	}
	return modelModule.NewRerankModel(driver, &name, config), nil
}
func (m *ModelProviderService) GetChatModelByIDWithContext(ctx context.Context, tenantID string, id int64) (*modelModule.ChatModel, error) {
	driver, name, config, err := m.getModelConfigByID(ctx, tenantID, id, entity.ModelTypeChat, nil)
	if err != nil {
		return nil, err
	}
	result := modelModule.NewChatModel(driver, &name, config)
	if m.providerManager != nil {
		var binding entity.TenantLLM
		if err := dao.DB.WithContext(ctx).Select("llm_factory").Where("id = ? AND tenant_id = ?", id, tenantID).First(&binding).Error; err != nil {
			return nil, err
		}
		if definition, err := m.providerManager.GetModelByName(binding.LLMFactory, name); err == nil {
			applyModelChatDefaults(definition, &result.ModelConfig)
		}
	}
	return result, nil
}
func (m *ModelProviderService) ResolvedEmbeddingIdentityByID(ctx context.Context, tenantID string, id int64) (EmbeddingModelIdentity, error) {
	var identity EmbeddingModelIdentity
	_, _, _, err := m.getModelConfigByID(ctx, tenantID, id, entity.ModelTypeEmbedding, &identity)
	return identity, err
}

func tenantDefaultBindingID(tenant *entity.Tenant, modelType entity.ModelType) *int64 {
	switch modelType {
	case entity.ModelTypeChat:
		return tenant.TenantLLMID
	case entity.ModelTypeEmbedding:
		return tenant.TenantEmbdID
	case entity.ModelTypeRerank:
		return tenant.TenantRerankID
	case entity.ModelTypeSpeech2Text:
		return tenant.TenantASRID
	case entity.ModelTypeImage2Text:
		return tenant.TenantImg2TxtID
	case entity.ModelTypeTTS:
		return tenant.TenantTTSID
	}
	return nil
}

// ListAllModels returns static metadata and its total, without consulting tenant state.
// Pages are zero based; size zero requests the complete catalog.
func (m *ModelProviderService) ListAllModels(page, size int) ([]*entity.Model, int, error) {
	if page < 0 || size < 0 || (size == 0 && page != 0) || (size > 0 && page > math.MaxInt/size) {
		return nil, 0, fmt.Errorf("invalid catalog pagination")
	}
	if m.providerManager == nil {
		return nil, 0, fmt.Errorf("model catalog unavailable")
	}
	all := m.providerManager.ListAllModels()
	total := len(all)
	if size == 0 {
		if all == nil {
			all = []*entity.Model{}
		}
		return all, total, nil
	}
	// Check before multiplication so arbitrarily large pages cannot wrap.
	if total == 0 || page > (total-1)/size {
		return []*entity.Model{}, total, nil
	}
	start := page * size
	end := total
	if size < total-start {
		end = start + size
	}
	return all[start:end], total, nil
}

func (m *ModelProviderService) ShowModel(name string) (*entity.Model, error) {
	if strings.TrimSpace(name) == "" {
		return nil, fmt.Errorf("model name required")
	}
	if m.providerManager == nil {
		return nil, fmt.Errorf("model catalog unavailable")
	}
	return m.providerManager.GetModelByNameOrAlias(name)
}
