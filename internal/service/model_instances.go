package service

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"strings"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

type providerInstanceExtra struct {
	Region  string `json:"region"`
	BaseURL string `json:"base_url,omitempty"`
}

func decodeProviderInstanceExtra(raw string) (*providerInstanceExtra, error) {
	extra := &providerInstanceExtra{}
	if strings.TrimSpace(raw) != "" {
		if err := json.Unmarshal([]byte(raw), extra); err != nil {
			return nil, fmt.Errorf("invalid model instance extra: %w", err)
		}
	}
	extra.Region = normalizeModelRegion(extra.Region)
	extra.BaseURL = strings.TrimSpace(extra.BaseURL)
	if extra.BaseURL != "" {
		parsed, err := url.Parse(extra.BaseURL)
		if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
			return nil, fmt.Errorf("model base URL must be an HTTP(S) endpoint without credentials, query or fragment")
		}
	}
	return extra, nil
}
func encodeProviderInstanceExtra(region, baseURL string) (string, error) {
	raw, err := json.Marshal(providerInstanceExtra{Region: region, BaseURL: baseURL})
	if err != nil {
		return "", err
	}
	extra, err := decodeProviderInstanceExtra(string(raw))
	if err != nil {
		return "", err
	}
	raw, err = json.Marshal(extra)
	return string(raw), err
}
func instanceModelDriver(provider *entity.Provider, instance *entity.TenantModelInstance) (models.ModelDriver, *models.APIConfig, error) {
	if provider == nil {
		return nil, nil, fmt.Errorf("provider not found")
	}
	if instance.Status != "active" {
		return nil, nil, fmt.Errorf("model instance is disabled")
	}
	extra, err := decodeProviderInstanceExtra(instance.Extra)
	if err != nil {
		return nil, nil, err
	}
	baseURL := extra.BaseURL
	if baseURL == "" {
		baseURL = provider.URL[extra.Region]
		if baseURL == "" {
			baseURL = provider.URL["default"]
		}
	}
	if strings.TrimSpace(baseURL) == "" {
		return nil, nil, fmt.Errorf("model base URL is missing")
	}
	if instance.APIKey == "" && !strings.EqualFold(provider.Name, "vllm") {
		return nil, nil, fmt.Errorf("model API key is missing")
	}
	driver, err := models.NewModelFactory().CreateModelDriver(provider.Name, map[string]string{"default": baseURL, extra.Region: baseURL}, provider.URLSuffix)
	return driver, &models.APIConfig{APIKey: &instance.APIKey, Region: &extra.Region}, err
}

type customModelExtra struct {
	MaxTokens int   `json:"max_tokens"`
	Thinking  *bool `json:"thinking,omitempty"`
}

func decodeCustomModelExtra(raw string) (*customModelExtra, error) {
	extra := &customModelExtra{}
	if err := json.Unmarshal([]byte(raw), extra); err != nil {
		return nil, fmt.Errorf("invalid custom model extra: %w", err)
	}
	if extra.MaxTokens <= 0 {
		return nil, fmt.Errorf("custom model max_tokens must be positive")
	}
	return extra, nil
}
func normalizeCustomModelType(raw string) (entity.ModelType, error) {
	switch raw {
	case "vision":
		raw = string(entity.ModelTypeImage2Text)
	case "asr":
		raw = string(entity.ModelTypeSpeech2Text)
	}
	switch entity.ModelType(raw) {
	case entity.ModelTypeChat, entity.ModelTypeImage2Text, entity.ModelTypeSpeech2Text, entity.ModelTypeEmbedding, entity.ModelTypeRerank, entity.ModelTypeTTS, entity.ModelTypeOCR:
		return entity.ModelType(raw), nil
	default:
		return "", fmt.Errorf("invalid model type")
	}
}

var errCustomModelExists = errors.New("model already exists")
var errProviderInstanceExists = errors.New("provider instance already exists")

type AddCustomModelRequest struct {
	ProviderName string `json:"provider_name"`
	InstanceName string `json:"instance_name"`
	ModelName    string `json:"model_name" binding:"required"`
	ModelType    string `json:"model_type" binding:"required"`
	MaxTokens    int    `json:"max_tokens"`
	Thinking     *bool  `json:"thinking"`
}

func (m *ModelProviderService) AddCustomModel(request *AddCustomModelRequest, userID string) (common.ErrorCode, error) {
	typ, err := normalizeCustomModelType(request.ModelType)
	if err != nil || request.MaxTokens <= 0 || strings.TrimSpace(request.ModelName) == "" || strings.Contains(request.ModelName, "@") {
		return common.CodeBadRequest, fmt.Errorf("valid model name, type and positive max_tokens are required")
	}
	if request.Thinking != nil && typ != entity.ModelTypeChat && typ != entity.ModelTypeImage2Text {
		return common.CodeBadRequest, fmt.Errorf("thinking requires chat or image2text")
	}
	tenants, err := m.userTenantDAO.GetByUserIDAndRole(userID, "owner")
	if err != nil {
		return common.CodeServerError, err
	}
	if len(tenants) == 0 {
		return common.CodeNotFound, fmt.Errorf("user has no tenants")
	}
	provider, err := m.modelProviderDAO.GetByTenantIDAndProviderName(tenants[0].TenantID, request.ProviderName)
	if err != nil {
		return common.CodeNotFound, err
	}
	instance, err := m.modelInstanceDAO.GetByProviderIDAndInstanceName(provider.ID, request.InstanceName)
	if err != nil {
		return common.CodeNotFound, err
	}
	if instance.Status != "active" {
		return common.CodeDataError, fmt.Errorf("model instance is disabled")
	}
	if m.providerManager.FindProvider(request.ProviderName) == nil {
		return common.CodeNotFound, fmt.Errorf("provider not found")
	}
	if _, err := m.providerManager.GetModelByName(request.ProviderName, request.ModelName); err == nil {
		return common.CodeConflict, fmt.Errorf("model already exists in provider catalog")
	}
	id, err := generateUUID1Hex()
	if err != nil {
		return common.CodeServerError, err
	}
	extra, err := json.Marshal(customModelExtra{MaxTokens: request.MaxTokens, Thinking: request.Thinking})
	if err != nil {
		return common.CodeServerError, err
	}
	model := &entity.TenantModel{ID: id, ModelName: request.ModelName, ProviderID: provider.ID, InstanceID: instance.ID, ModelType: string(typ), Status: "active", Extra: string(extra)}
	err = dao.DB.Transaction(func(tx *gorm.DB) error {
		// Serialize declarations for this owned instance without changing the schema.
		var locked entity.TenantModelInstance
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ? AND provider_id = ?", instance.ID, provider.ID).First(&locked).Error; err != nil {
			return err
		}
		if locked.Status != "active" {
			return fmt.Errorf("model instance is disabled")
		}
		var existing entity.TenantModel
		err := tx.Where("provider_id = ? AND instance_id = ? AND model_name = ?", provider.ID, instance.ID, request.ModelName).First(&existing).Error
		if err == nil {
			return errCustomModelExists
		}
		if !errors.Is(err, gorm.ErrRecordNotFound) {
			return err
		}
		return tx.Create(model).Error
	})
	if errors.Is(err, errCustomModelExists) {
		return common.CodeConflict, err
	}
	if err != nil {
		return common.CodeServerError, err
	}
	return common.CodeSuccess, nil
}
func (m *ModelProviderService) instanceModelDefinition(provider *entity.Provider, providerID, instanceID, name string) (*entity.Model, error) {
	stored, err := m.modelDAO.GetModelByProviderIDAndInstanceIDAndModelName(providerID, instanceID, name)
	if err != nil {
		if !errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, err
		}
		return m.providerManager.GetModelByName(provider.Name, name)
	}
	if stored.Status != "active" {
		return nil, fmt.Errorf("model is disabled")
	}
	// Old rows are catalog disable markers regardless of their legacy status value.
	if stored.Extra == "" || stored.Extra == "{}" {
		return nil, fmt.Errorf("model is disabled")
	}
	extra, err := decodeCustomModelExtra(stored.Extra)
	if err != nil {
		return nil, err
	}
	typ, err := normalizeCustomModelType(stored.ModelType)
	if err != nil {
		return nil, err
	}
	class := provider.Class
	model := &entity.Model{Name: name, MaxTokens: extra.MaxTokens, ModelTypes: []string{string(typ)}, Class: &class}
	if extra.Thinking != nil {
		model.Thinking = &entity.ModelThinking{DefaultValue: *extra.Thinking}
	}
	return model, nil
}
func (m *ModelProviderService) userInstanceChatModel(providerName, instanceName, modelName, userID string, apiConfig *models.APIConfig, config *models.ChatConfig) (*models.ChatModel, common.ErrorCode, error) {
	for _, part := range []string{providerName, instanceName, modelName} {
		if strings.TrimSpace(part) == "" || strings.Contains(part, "@") {
			return nil, common.CodeBadRequest, fmt.Errorf("invalid model identifier")
		}
	}
	tenants, err := m.userTenantDAO.GetByUserIDAndRole(userID, "owner")
	if err != nil {
		return nil, common.CodeServerError, err
	}
	if len(tenants) == 0 {
		return nil, common.CodeNotFound, fmt.Errorf("user has no tenants")
	}
	bound, err := m.GetChatModel(tenants[0].TenantID, modelName+"@"+instanceName+"@"+providerName)
	if err != nil {
		return nil, common.CodeDataError, err
	}
	if apiConfig != nil {
		bound.APIConfig.Context = apiConfig.Context
	}
	if config != nil {
		selected := *config
		if selected.Thinking == nil {
			selected.Thinking = bound.ModelConfig.Thinking
		}
		if selected.MaxTokens == nil {
			selected.MaxTokens = bound.ModelConfig.MaxTokens
		}
		selected.ModelClass = bound.ModelConfig.ModelClass
		bound.ModelConfig = selected
	}
	return bound, common.CodeSuccess, nil
}

func createProviderInstanceRow(providerID string, instance *entity.TenantModelInstance) error {
	return dao.DB.Transaction(func(tx *gorm.DB) error {
		var provider entity.TenantModelProvider
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ?", providerID).First(&provider).Error; err != nil {
			return err
		}
		var existing entity.TenantModelInstance
		err := tx.Where("provider_id = ? AND instance_name = ?", providerID, instance.InstanceName).First(&existing).Error
		if err == nil {
			return errProviderInstanceExists
		}
		if !errors.Is(err, gorm.ErrRecordNotFound) {
			return err
		}
		return tx.Create(instance).Error
	})
}
