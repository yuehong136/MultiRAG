package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"slices"
	"strings"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

var (
	errModelDisabled         = errors.New("model is disabled")
	errModelInstanceDisabled = errors.New("model instance is disabled")
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
func instanceModelDriver(provider *entity.Provider, instance *entity.TenantModelInstance, factories ...models.DriverFactory) (models.ModelDriver, *models.APIConfig, error) {
	if provider == nil || instance == nil {
		return nil, nil, fmt.Errorf("provider not found")
	}
	if instance.Status != "active" {
		return nil, nil, errModelInstanceDisabled
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
	if strings.TrimSpace(baseURL) == "" && !strings.EqualFold(provider.Name, "Bedrock") {
		return nil, nil, fmt.Errorf("model base URL is missing")
	}
	if instance.APIKey == "" && !strings.EqualFold(provider.Name, "vllm") && !strings.EqualFold(provider.Name, "ollama") && !strings.EqualFold(provider.Name, "LM-Studio") && !strings.EqualFold(provider.Name, "LocalAI") && !strings.EqualFold(provider.Name, "Xinference") && !strings.EqualFold(provider.Name, "mineru_local") && !strings.EqualFold(provider.Name, "PaddleOCR_Local") && !strings.EqualFold(provider.Name, "ModelScope") {
		return nil, nil, fmt.Errorf("model API key is missing")
	}
	var factory models.DriverFactory = models.NewModelFactory()
	if len(factories) > 0 && factories[0] != nil {
		factory = factories[0]
	}
	// Bedrock derives native runtime/control endpoints from validated credentials
	// when no instance override exists. Other drivers still require an endpoint.
	urls := map[string]string{}
	if baseURL != "" {
		urls["default"] = baseURL
		urls[extra.Region] = baseURL
	}
	driver, err := factory.CreateModelDriver(provider.Name, urls, provider.URLSuffix)
	return driver, &models.APIConfig{APIKey: &instance.APIKey, Region: &extra.Region}, err
}

type customModelExtra struct {
	MaxTokens  int      `json:"max_tokens"`
	Thinking   *bool    `json:"thinking,omitempty"`
	ModelTypes []string `json:"model_types,omitempty"`
}

func decodeCustomModelExtra(raw string) (*customModelExtra, error) {
	extra := &customModelExtra{}
	if err := json.Unmarshal([]byte(raw), extra); err != nil {
		return nil, fmt.Errorf("invalid custom model extra: %w", err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal([]byte(raw), &fields); err != nil || fields["max_tokens"] == nil || string(fields["max_tokens"]) == "null" || extra.MaxTokens < 0 {
		return nil, fmt.Errorf("custom model max_tokens must be a nonnegative integer")
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
	case entity.ModelTypeChat, entity.ModelTypeImage2Text, entity.ModelTypeSpeech2Text, entity.ModelTypeEmbedding, entity.ModelTypeRerank, entity.ModelTypeTTS, entity.ModelTypeOCR, documentParseType:
		return entity.ModelType(raw), nil
	default:
		return "", fmt.Errorf("invalid model type")
	}
}

var errCustomModelExists = errors.New("model already exists")
var errProviderInstanceExists = errors.New("provider instance already exists")

type AddCustomModelRequest struct {
	ProviderName string   `json:"provider_name"`
	InstanceName string   `json:"instance_name"`
	ModelName    string   `json:"model_name" binding:"required"`
	ModelType    string   `json:"model_type"`
	ModelTypes   []string `json:"model_types"`
	MaxTokens    int      `json:"max_tokens"`
	Thinking     *bool    `json:"thinking"`
}

// UnmarshalJSON distinguishes a legal zero token limit from a missing value.
func (r *AddCustomModelRequest) UnmarshalJSON(data []byte) error {
	type declaration AddCustomModelRequest
	var payload struct {
		*declaration
		MaxTokens *int `json:"max_tokens"`
	}
	payload.declaration = (*declaration)(r)
	// The wire declaration must not reinterpret explicit null inventories or
	// duplicate fields as an omitted capability list.
	fields, err := strictJSONObject(data)
	if err != nil {
		return fmt.Errorf("invalid model declaration")
	}
	for _, key := range []string{"model_name", "model_type", "model_types", "provider_name", "instance_name"} {
		if raw, exists := fields[key]; exists && strings.TrimSpace(string(raw)) == "null" {
			return fmt.Errorf("invalid model declaration")
		}
	}
	if err := json.Unmarshal(data, &payload); err != nil {
		return fmt.Errorf("invalid model declaration")
	}
	if payload.MaxTokens == nil {
		return fmt.Errorf("max_tokens is required")
	}
	r.MaxTokens = *payload.MaxTokens
	return nil
}

// AddCustomModel remains the single-declaration entry used by the CLI consumer.
func (m *ModelProviderService) AddCustomModel(request *AddCustomModelRequest, userID string) (common.ErrorCode, error) {
	if request == nil {
		return common.CodeBadRequest, fmt.Errorf("request is required")
	}
	return m.AddModels(context.Background(), request.ProviderName, request.InstanceName, []AddCustomModelRequest{*request}, userID)
}

// AddModels declares the complete batch atomically under the owned instance lock.
func (m *ModelProviderService) AddModels(ctx context.Context, providerName, instanceName string, requests []AddCustomModelRequest, userID string) (common.ErrorCode, error) {
	if providerName == "" || providerName != strings.TrimSpace(providerName) || instanceName == "" || instanceName != strings.TrimSpace(instanceName) || strings.Contains(instanceName, "@") || len(requests) == 0 || len(requests) > 100 {
		return common.CodeBadRequest, fmt.Errorf("valid provider, instance and 1 to 100 models are required")
	}
	modelsToCreate := make([]entity.TenantModel, 0, len(requests))
	seen := make(map[string]bool, len(requests))
	for _, request := range requests {
		if request.ProviderName != "" && request.ProviderName != providerName || request.InstanceName != "" && request.InstanceName != instanceName {
			return common.CodeBadRequest, fmt.Errorf("model declaration does not match route")
		}
		types, err := normalizeCustomModelTypes(request.ModelType, request.ModelTypes)
		if err != nil || request.MaxTokens < 0 || strings.TrimSpace(request.ModelName) == "" || request.ModelName != strings.TrimSpace(request.ModelName) || strings.Contains(request.ModelName, "@") || seen[request.ModelName] {
			return common.CodeBadRequest, fmt.Errorf("valid unique model names, types and nonnegative max_tokens are required")
		}
		if request.Thinking != nil && !slices.Contains(types, string(entity.ModelTypeChat)) && !slices.Contains(types, string(entity.ModelTypeImage2Text)) {
			return common.CodeBadRequest, fmt.Errorf("thinking requires chat or image2text")
		}
		seen[request.ModelName] = true
		extra, err := json.Marshal(customModelExtra{MaxTokens: request.MaxTokens, Thinking: request.Thinking, ModelTypes: types})
		if err != nil {
			return common.CodeServerError, err
		}
		id, err := generateUUID1Hex()
		if err != nil {
			return common.CodeServerError, err
		}
		modelsToCreate = append(modelsToCreate, entity.TenantModel{ID: id, ModelName: request.ModelName, ModelType: types[0], Status: "active", Extra: string(extra)})
	}
	err := dao.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var owner entity.UserTenant
		if err := tx.Where("user_id = ? AND role = ? AND status = ?", userID, "owner", "1").First(&owner).Error; err != nil {
			return err
		}
		var provider entity.TenantModelProvider
		if err := tx.Where("tenant_id = ? AND provider_name = ?", owner.TenantID, providerName).First(&provider).Error; err != nil {
			return err
		}
		var instance entity.TenantModelInstance
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("provider_id = ? AND instance_name = ?", provider.ID, instanceName).First(&instance).Error; err != nil {
			return err
		}
		if instance.Status != "active" {
			return errModelInstanceDisabled
		}
		if m.providerManager.FindProvider(providerName) == nil {
			return gorm.ErrRecordNotFound
		}
		for i := range modelsToCreate {
			model := &modelsToCreate[i]
			if _, err := m.providerManager.GetModelByName(providerName, model.ModelName); err == nil {
				return errCustomModelExists
			}
			var count int64
			if err := tx.Model(&entity.TenantModel{}).Where("provider_id = ? AND instance_id = ? AND model_name = ?", provider.ID, instance.ID, model.ModelName).Count(&count).Error; err != nil {
				return err
			}
			if count > 0 {
				return errCustomModelExists
			}
			model.ProviderID, model.InstanceID = provider.ID, instance.ID
		}
		return tx.Create(&modelsToCreate).Error
	})
	if errors.Is(err, errCustomModelExists) {
		return common.CodeConflict, err
	}
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return common.CodeNotFound, fmt.Errorf("provider instance not found")
	}
	if errors.Is(err, errModelInstanceDisabled) {
		return common.CodeDataError, err
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
		return nil, errModelDisabled
	}
	// Old rows are catalog disable markers regardless of their legacy status value.
	if stored.Extra == "" || stored.Extra == "{}" {
		return nil, errModelDisabled
	}
	extra, err := decodeCustomModelExtra(stored.Extra)
	if err != nil {
		return nil, err
	}
	types, err := normalizeCustomModelTypes(stored.ModelType, extra.ModelTypes)
	if err != nil {
		return nil, err
	}
	class := provider.Class
	model := &entity.Model{Name: name, MaxTokens: extra.MaxTokens, ModelTypes: types, Class: &class}
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

// Keep the first capability in ModelType for existing single-type consumers.
// Older rows without Extra.model_types retain their original capability.
func normalizeCustomModelTypes(primary string, declared []string) ([]string, error) {
	if declared == nil {
		typ, err := normalizeCustomModelType(primary)
		if err != nil {
			return nil, err
		}
		return []string{string(typ)}, nil
	}
	if len(declared) == 0 {
		return nil, fmt.Errorf("model_types must not be empty")
	}
	result := make([]string, 0, len(declared))
	for _, raw := range declared {
		typ, err := normalizeCustomModelType(raw)
		if err != nil {
			return nil, err
		}
		if slices.Contains(result, string(typ)) {
			return nil, fmt.Errorf("duplicate model capability")
		}
		result = append(result, string(typ))
	}
	if primary != "" {
		typ, err := normalizeCustomModelType(primary)
		if err != nil || string(typ) != result[0] {
			return nil, fmt.Errorf("model_type must match first model_types capability")
		}
	}
	return result, nil
}
