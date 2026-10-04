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
	if strings.TrimSpace(instanceName) == "" || strings.Contains(instanceName, "@") || instanceName == "default" {
		return common.CodeBadRequest, fmt.Errorf("invalid instance name")
	}
	if strings.TrimSpace(apiKey) == "" && !strings.EqualFold(providerName, "vllm") {
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
	return common.CodeSuccess, nil
}
func (m *ModelProviderService) DropProviderInstances(providerName, userID string, instances []string) (common.ErrorCode, error) {

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

	for _, instanceName := range instances {
		count, err := m.modelInstanceDAO.DeleteByProviderIDAndInstanceName(provider.ID, instanceName)
		if err != nil {
			return common.CodeServerError, err
		}

		if count == 0 {
			return common.CodeNotFound, errors.New("provider instance not found")
		}
	}

	return common.CodeSuccess, nil
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

	instance, err := m.modelInstanceDAO.GetByProviderIDAndInstanceName(provider.ID, instanceName)
	if err != nil {
		return common.CodeServerError, err
	}

	active := status == "enable" || status == "enabled" || status == "active"
	if !active && status != "disable" && status != "disabled" && status != "inactive" {
		return common.CodeBadRequest, fmt.Errorf("invalid model status")
	}
	model, err := m.modelDAO.GetModelByProviderIDAndInstanceIDAndModelName(provider.ID, instance.ID, modelName)
	if err != nil && !errors.Is(err, gorm.ErrRecordNotFound) {
		return common.CodeServerError, err
	}
	if err == nil {
		if model.Extra != "" && model.Extra != "{}" {
			state := "inactive"
			if active {
				state = "active"
			}
			err = dao.DB.Model(model).Update("status", state).Error
		} else if active {
			_, err = m.modelDAO.DeleteByModelID(model.ID)
		}
		if err != nil {
			return common.CodeServerError, err
		}
		return common.CodeSuccess, nil
	}
	schema, err := m.providerManager.GetModelByName(providerName, modelName)
	if err != nil {
		return common.CodeNotFound, err
	}
	if active {
		return common.CodeSuccess, nil
	}
	id, err := generateUUID1Hex()
	if err != nil {
		return common.CodeServerError, err
	}
	model = &entity.TenantModel{ID: id, ProviderID: provider.ID, InstanceID: instance.ID, ModelName: modelName, ModelType: schema.ModelTypes[0], Status: status}
	if err := m.modelDAO.Create(model); err != nil {
		return common.CodeServerError, err
	}
	return common.CodeSuccess, nil
}

func (m *ModelProviderService) ChatToModel(providerName, instanceName, modelName, userID, message string, apiConfig *modelModule.APIConfig, modelConfig *modelModule.ChatConfig) (*modelModule.ChatResponse, common.ErrorCode, error) {
	bound, code, err := m.userInstanceChatModel(providerName, instanceName, modelName, userID, apiConfig, modelConfig)
	if err != nil {
		return nil, code, err
	}
	response, err := bound.ModelDriver.Chat(bound.ModelName, &message, bound.APIConfig, &bound.ModelConfig)
	if err != nil {
		return nil, common.CodeServerError, err
	}
	return response, common.CodeSuccess, nil
}

func (m *ModelProviderService) ChatToModelByAPIKey(providerName, modelName, apiKey, message string) (*string, common.ErrorCode, error) {
	providerInfo := dao.GetModelProviderManager().FindProvider(providerName)
	if providerInfo == nil {
		return nil, common.CodeNotFound, errors.New("provider not found")
	}
	model, err := dao.GetModelProviderManager().GetModelByName(providerName, modelName)
	if err != nil {
		return nil, common.CodeNotFound, fmt.Errorf("provider %s model %s not found", providerName, modelName)
	}

	config := &modelModule.ChatConfig{}
	applyModelChatDefaults(model, config)
	apiConfig := &modelModule.APIConfig{APIKey: &apiKey}
	response, err := providerInfo.ModelDriver.Chat(&modelName, &message, apiConfig, config)
	if err != nil {
		return nil, common.CodeServerError, err
	}
	return response.Answer, common.CodeSuccess, nil
}

// ChatWithMessagesToModelByAPIKey sends multiple role-tagged messages and returns the response.
func (m *ModelProviderService) ChatWithMessagesToModelByAPIKey(providerName, modelName, apiKey string, messages []modelModule.Message) (*string, common.ErrorCode, error) {
	providerInfo := dao.GetModelProviderManager().FindProvider(providerName)
	if providerInfo == nil {
		return nil, common.CodeNotFound, errors.New("provider not found")
	}
	model, err := dao.GetModelProviderManager().GetModelByName(providerName, modelName)
	if err != nil {
		return nil, common.CodeNotFound, fmt.Errorf("provider %s model %s not found", providerName, modelName)
	}

	config := &modelModule.ChatConfig{}
	applyModelChatDefaults(model, config)
	response, err := providerInfo.ModelDriver.ChatWithMessages(modelName, &modelModule.APIConfig{APIKey: &apiKey}, messages, config)
	if err != nil {
		return nil, common.CodeServerError, err
	}
	return &response, common.CodeSuccess, nil
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
	bound, code, err := m.userInstanceChatModel(providerName, instanceName, modelName, userID, apiConfig, modelConfig)
	if err != nil {
		return code, err
	}
	if err := bound.ModelDriver.ChatStreamlyWithSender(bound.ModelName, &message, bound.APIConfig, &bound.ModelConfig, sender); err != nil {
		return common.CodeServerError, err
	}
	return common.CodeSuccess, nil
}

func (m *ModelProviderService) GetDefaultModel(modelType entity.ModelType, tenantID string) (*entity.ModelCredentials, error) {
	// Get tenant record to find default model name
	tenant, err := dao.NewTenantDAO().GetByID(tenantID)
	if err != nil {
		return nil, fmt.Errorf("tenant not found: %w", err)
	}

	// Determine model name based on model type
	var defaultModelName string
	switch modelType {
	case entity.ModelTypeChat:
		defaultModelName = tenant.LLMID
	case entity.ModelTypeEmbedding:
		defaultModelName = tenant.EmbdID
	case entity.ModelTypeSpeech2Text:
		defaultModelName = tenant.ASRID
	case entity.ModelTypeImage2Text:
		defaultModelName = tenant.Img2TxtID
	case entity.ModelTypeRerank:
		defaultModelName = tenant.RerankID
	case entity.ModelTypeTTS:
		if tenant.TTSID != nil {
			defaultModelName = *tenant.TTSID
		}
	case entity.ModelTypeOCR:
		return nil, errors.New("OCR model name is required")
	default:
		return nil, fmt.Errorf("unknown model type: %s", modelType)
	}

	if defaultModelName == "" {
		return nil, fmt.Errorf("no default %s model is set", modelType)
	}

	// Look up the TenantLLM record to get provider name and API key
	// Use GetByTenantIDAndLLMName which handles splitting model name and factory
	tenantLLM, err := dao.NewTenantLLMDAO().GetByTenantIDAndLLMName(tenantID, defaultModelName)
	if err != nil {
		return nil, fmt.Errorf("failed to get tenant default model: %w", err)
	}

	if tenantLLM == nil {
		return nil, fmt.Errorf("no default %s model found for tenant", modelType)
	}

	if tenantLLM.LLMName == nil || tenantLLM.APIKey == nil {
		return nil, fmt.Errorf("tenant model %q has missing name or api key", defaultModelName)
	}
	return &entity.ModelCredentials{
		ProviderName: tenantLLM.LLMFactory,
		ModelName:    *tenantLLM.LLMName,
		APIKey:       *tenantLLM.APIKey,
	}, nil
}

// GetModelByName gets model credentials by model name (chat_id from search_config)
func (m *ModelProviderService) GetModelByName(modelName string, tenantID string) (*entity.ModelCredentials, error) {
	tenantLLM, err := dao.NewTenantLLMDAO().GetByTenantIDAndLLMName(tenantID, modelName)
	if err != nil {
		return nil, fmt.Errorf("failed to get model by name: %w", err)
	}
	if tenantLLM == nil {
		return nil, fmt.Errorf("model not found: %s", modelName)
	}

	return &entity.ModelCredentials{
		ProviderName: tenantLLM.LLMFactory,
		ModelName:    *tenantLLM.LLMName,
		APIKey:       *tenantLLM.APIKey,
	}, nil
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
		tenant, err := dao.NewTenantDAO().GetByID(tenantID)
		if err != nil {
			return nil, "", nil, err
		}
		switch modelType {
		case entity.ModelTypeEmbedding:
			compositeName = tenant.EmbdID
		case entity.ModelTypeRerank:
			compositeName = tenant.RerankID
		case entity.ModelTypeChat:
			compositeName = tenant.LLMID
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
		return nil, "", nil, fmt.Errorf("model instance is disabled")
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
		return nil, "", nil, fmt.Errorf("model is disabled")
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
