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
	"errors"
	"fmt"
	"strings"
	"time"

	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	modelModule "multirag/internal/entity/models"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

func NewModelProviderService() *ModelProviderService {
	return &ModelProviderService{
		providerManager:      dao.GetModelProviderManager(),
		modelProviderDAO:     dao.NewTenantModelProviderDAO(),
		modelInstanceDAO:     dao.NewTenantModelInstanceDAO(),
		modelDAO:             dao.NewTenantModelDAO(),
		modelGroupDAO:        dao.NewTenantModelGroupDAO(),
		modelGroupMappingDAO: dao.NewTenantModelGroupMappingDAO(),
		userTenantDAO:        dao.NewUserTenantDAO(),
	}
}

type ModelProviderService struct {
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

	now := time.Now().Unix()
	nowDate := time.Now().Truncate(time.Second)
	tenantModelProvider := &entity.TenantModelProvider{
		ID:           providerID,
		ProviderName: providerName,
		TenantID:     tenantID,
	}
	tenantModelProvider.CreateTime = &now
	tenantModelProvider.UpdateTime = &now
	tenantModelProvider.CreateDate = &nowDate
	tenantModelProvider.UpdateDate = &nowDate
	err = m.modelProviderDAO.Create(tenantModelProvider)
	if err != nil {
		return common.CodeServerError, errors.New("fail to create model provider")
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

func (m *ModelProviderService) ListSupportedModels(providerName, instanceName, userID string) ([]string, error) {
	tenants, err := m.userTenantDAO.GetByUserIDAndRole(userID, "owner")
	if err != nil {
		return nil, errors.New("fail to get tenant")
	}
	if len(tenants) == 0 {
		return nil, errors.New("user has no tenants")
	}

	provider, err := m.modelProviderDAO.GetByTenantIDAndProviderName(tenants[0].TenantID, providerName)
	if err != nil {
		return nil, err
	}
	instance, err := m.modelInstanceDAO.GetByProviderIDAndInstanceName(provider.ID, instanceName)
	if err != nil {
		return nil, err
	}
	providerInfo := dao.GetModelProviderManager().FindProvider(providerName)
	if providerInfo == nil {
		return nil, fmt.Errorf("provider %s not found", providerName)
	}
	driver, apiConfig, err := instanceModelDriver(providerInfo, instance)
	if err != nil {
		return nil, err
	}
	return driver.ListModels(apiConfig)
}

func (m *ModelProviderService) CreateProviderInstance(providerName, instanceName, apiKey, userID, region string, baseURLs ...string) (common.ErrorCode, error) {
	if strings.TrimSpace(instanceName) == "" || strings.Contains(instanceName, "@") {
		return common.CodeBadRequest, fmt.Errorf("invalid instance name")
	}
	if strings.TrimSpace(apiKey) == "" && !strings.EqualFold(providerName, "vllm") && !strings.EqualFold(providerName, "ollama") {
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

	now := time.Now().Unix()
	nowDate := time.Now().Truncate(time.Second)
	tenantModelProvider := &entity.TenantModelInstance{
		ID:           instanceID,
		InstanceName: instanceName,
		ProviderID:   provider.ID,
		APIKey:       apiKey,
		Status:       "active",
		Extra:        extra,
	}
	tenantModelProvider.CreateTime = &now
	tenantModelProvider.UpdateTime = &now
	tenantModelProvider.CreateDate = &nowDate
	tenantModelProvider.UpdateDate = &nowDate
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

func (m *ModelProviderService) ShowInstanceBalance(providerName, instanceName, userID string) (map[string]interface{}, common.ErrorCode, error) {

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

	driver, apiConfig, err := instanceModelDriver(providerInfo, instance)
	if err != nil {
		return nil, common.CodeDataError, err
	}
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
	driver, name, config, err := m.getModelConfig(tenantID, compositeName, entity.ModelTypeEmbedding)
	if err != nil {
		return nil, err
	}
	return modelModule.NewEmbeddingModel(driver, &name, config), nil
}
func (m *ModelProviderService) GetRerankModel(tenantID, compositeName string) (*modelModule.RerankModel, error) {
	driver, name, config, err := m.getModelConfig(tenantID, compositeName, entity.ModelTypeRerank)
	if err != nil {
		return nil, err
	}
	return modelModule.NewRerankModel(driver, &name, config), nil
}
func (m *ModelProviderService) GetChatModel(tenantID, compositeName string) (*modelModule.ChatModel, error) {
	defaults := modelModule.ChatConfig{}
	driver, name, config, err := m.getModelConfig(tenantID, compositeName, entity.ModelTypeChat, &defaults)
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
	if compositeName == "" {
		var err error
		compositeName, err = NewTenantService().GetDefaultModelName(tenantID, modelType)
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
	provider, err := m.modelProviderDAO.GetByTenantIDAndProviderName(tenantID, providerName)
	if errors.Is(err, gorm.ErrRecordNotFound) && strings.Count(compositeName, "@") == 1 {
		return m.getLegacyModelConfig(tenantID, providerName, name, modelType, providerInfo)
	}

	if err != nil {
		return nil, "", nil, err
	}
	instance, err := m.modelInstanceDAO.GetByProviderIDAndInstanceName(provider.ID, instanceName)
	if errors.Is(err, gorm.ErrRecordNotFound) && strings.Count(compositeName, "@") == 1 {
		return m.getLegacyModelConfig(tenantID, providerName, name, modelType, providerInfo)
	}
	if err != nil {
		return nil, "", nil, err
	}
	if instance.Status != "active" {
		return nil, "", nil, errModelInstanceDisabled
	}
	model, err := m.instanceModelDefinition(providerInfo, provider.ID, instance.ID, name)
	if err != nil {
		return nil, "", nil, err
	}
	supported := false
	for _, typ := range model.ModelTypes {
		if typ == string(modelType) {
			supported = true
		}
	}
	if !supported {
		return nil, "", nil, fmt.Errorf("model type mismatch")
	}
	if len(chatDefaults) > 0 {
		applyModelChatDefaults(model, chatDefaults[0])
	}
	driver, config, err := instanceModelDriver(providerInfo, instance)
	return driver, name, config, err
}

// getLegacyModelConfig keeps old tenant credentials usable while removing the
// duplicate provider implementation. Only a missing provider/default instance
// can select this path; database failures and disabled models remain errors.
func (m *ModelProviderService) getLegacyModelConfig(tenantID, providerName, name string, modelType entity.ModelType, providerInfo *entity.Provider) (modelModule.ModelDriver, string, *modelModule.APIConfig, error) {
	legacy, err := dao.NewTenantLLMDAO().GetByTenantFactoryAndModelName(tenantID, providerName, name)
	if err != nil {
		return nil, "", nil, err
	}
	if legacy.Status == "0" {
		return nil, "", nil, errModelDisabled
	}
	if legacy.ModelType != nil && *legacy.ModelType != string(modelType) {
		return nil, "", nil, fmt.Errorf("model type mismatch")
	}
	if legacy.APIKey == nil || *legacy.APIKey == "" {
		return nil, "", nil, fmt.Errorf("model API key is missing")
	}
	urls := providerInfo.URL
	if legacy.APIBase != nil && *legacy.APIBase != "" {
		urls = map[string]string{"default": *legacy.APIBase}
	}
	if strings.TrimSpace(urls["default"]) == "" {
		return nil, "", nil, fmt.Errorf("model base URL is missing")
	}
	driver, err := modelModule.NewModelFactory().CreateModelDriver(providerInfo.Name, urls, providerInfo.URLSuffix)
	return driver, name, &modelModule.APIConfig{APIKey: legacy.APIKey}, err
}
