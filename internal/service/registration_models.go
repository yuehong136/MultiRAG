package service

import (
	"encoding/json"
	"errors"
	"fmt"
	"gorm.io/gorm"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"strings"
)

// InitializeAdminTenantModels retains the current configured instances and adds
// the numeric bindings still consumed by tenant settings and memory. It runs in
// the admin account-creation transaction and never seeds the full model catalog.
// Public registration deliberately keeps InitializeTenantModels' existing scope.
func InitializeAdminTenantModels(tx *gorm.DB, tenant *entity.Tenant, models server.DefaultModelsConfig) error {
	if tx == nil || tenant == nil || tenant.ID == "" {
		return fmt.Errorf("tenant registration transaction is required")
	}
	if err := InitializeTenantModels(tx, tenant, models); err != nil {
		return err
	}
	entries := []struct {
		kind, column string
		config       server.ModelConfig
		selector     string
		target       **int64
	}{
		{"chat", "tenant_llm_id", models.ChatModel, tenant.LLMID, &tenant.TenantLLMID},
		{"embedding", "tenant_embd_id", models.EmbeddingModel, tenant.EmbdID, &tenant.TenantEmbdID},
		{"rerank", "tenant_rerank_id", models.RerankModel, tenant.RerankID, &tenant.TenantRerankID},
		{"speech2text", "tenant_asr_id", models.ASRModel, tenant.ASRID, &tenant.TenantASRID},
		{"image2text", "tenant_img2txt_id", models.Image2TextModel, tenant.Img2TxtID, &tenant.TenantImg2TxtID},
	}
	updates := map[string]any{}
	for _, entry := range entries {
		if entry.selector == "" {
			continue
		}
		name, factory := NewTenantLLMService().SplitModelNameAndFactory(entry.selector)
		if factory != entry.config.Factory || name == "" {
			return fmt.Errorf("configured model selector is inconsistent")
		}
		definition, err := dao.GetModelProviderManager().GetModelByName(factory, entry.config.Name)
		if err != nil || definition == nil || definition.MaxTokens < 0 {
			return fmt.Errorf("configured model catalog is unavailable")
		}
		var binding entity.TenantLLM
		err = tx.Where("tenant_id = ? AND llm_factory = ? AND llm_name = ?", tenant.ID, factory, name).Take(&binding).Error
		if errors.Is(err, gorm.ErrRecordNotFound) {
			kind, key, base := entry.kind, entry.config.APIKey, entry.config.BaseURL
			binding = entity.TenantLLM{TenantID: tenant.ID, LLMFactory: factory, ModelType: &kind, LLMName: &name, APIKey: &key, APIBase: &base, MaxTokens: int64(definition.MaxTokens), Status: "1"}
			if err := tx.Create(&binding).Error; err != nil {
				return err
			}
			// GORM's tagged default replaces a legal zero during struct Create.
			// Write the catalog value explicitly inside this same transaction.
			if err := tx.Model(&binding).UpdateColumn("max_tokens", int64(definition.MaxTokens)).Error; err != nil {
				return err
			}
			binding.MaxTokens = int64(definition.MaxTokens)
		} else if err != nil {
			return err
		} else if binding.ID <= 0 || binding.Status != "1" || binding.ModelType == nil || *binding.ModelType != entry.kind || binding.APIKey == nil || *binding.APIKey != entry.config.APIKey || binding.APIBase == nil || *binding.APIBase != entry.config.BaseURL || binding.MaxTokens != int64(definition.MaxTokens) {
			return fmt.Errorf("existing configured model binding is unavailable or inconsistent")
		}
		if binding.ID <= 0 {
			return fmt.Errorf("configured model numeric binding was not assigned")
		}
		id := binding.ID
		*entry.target = &id
		updates[entry.column] = id
	}
	if len(updates) == 0 {
		return nil
	}
	return tx.Model(tenant).Updates(updates).Error
}

// InitializeTenantModels uses only the configured defaults and their own credentials.
// All rows and default references participate in the registration transaction.
func InitializeTenantModels(tx *gorm.DB, tenant *entity.Tenant, models server.DefaultModelsConfig) error {
	providers := map[string]string{}
	entries := []struct {
		typ    string
		config server.ModelConfig
		target *string
	}{
		{"chat", models.ChatModel, &tenant.LLMID}, {"embedding", models.EmbeddingModel, &tenant.EmbdID}, {"rerank", models.RerankModel, &tenant.RerankID}, {"asr", models.ASRModel, &tenant.ASRID}, {"image2text", models.Image2TextModel, &tenant.Img2TxtID},
	}
	for _, entry := range entries {
		cfg := entry.config
		*entry.target = ""
		if cfg.Name == "" && cfg.Factory == "" {
			continue
		}
		if cfg.Name == "" || cfg.Factory == "" || strings.Contains(cfg.Name+cfg.Factory, "@") {
			return fmt.Errorf("incomplete default model configuration")
		}
		manager := dao.GetModelProviderManager()
		if manager == nil {
			return fmt.Errorf("model catalog unavailable")
		}
		definition, err := manager.GetModelByName(cfg.Factory, cfg.Name)
		if err != nil {
			return fmt.Errorf("configured default model not in catalog")
		}
		expected, err := normalizeCustomModelType(entry.typ)
		if err != nil {
			return err
		}
		supported := false
		for _, typ := range definition.ModelTypes {
			supported = supported || modelTypeMatches(typ, expected)
		}
		if !supported || definition.MaxTokens < 0 {
			return fmt.Errorf("configured default model capability unavailable")
		}
		modelExtra := customModelExtra{MaxTokens: definition.MaxTokens, ModelTypes: definition.ModelTypes}
		if definition.Thinking != nil {
			value := definition.Thinking.DefaultValue
			modelExtra.Thinking = &value
		}
		encodedModel, err := json.Marshal(modelExtra)
		if err != nil {
			return err
		}
		providerID := providers[cfg.Factory]
		if providerID == "" {
			providerID = common.GenerateUUID()
			row := entity.TenantModelProvider{ID: providerID, ProviderName: cfg.Factory, TenantID: tenant.ID}
			if err := tx.Create(&row).Error; err != nil {
				return err
			}
			providers[cfg.Factory] = providerID
		}
		extra, err := encodeProviderInstanceExtra("default", cfg.BaseURL)
		if err != nil {
			return err
		}
		instance := &entity.TenantModelInstance{ID: common.GenerateUUID(), ProviderID: providerID, InstanceName: "default-" + entry.typ, APIKey: cfg.APIKey, Status: "active", Extra: extra}
		if err := tx.Create(instance).Error; err != nil {
			return err
		}

		// A catalog declaration may legitimately omit a token limit (for example
		// Whisper ASR). Leave it catalog-backed: an empty override is a disable marker,
		// while customModelExtra correctly requires a positive explicit token budget.
		if definition.MaxTokens > 0 {
			model := entity.TenantModel{ID: common.GenerateUUID(), ProviderID: providerID, InstanceID: instance.ID, ModelName: cfg.Name, ModelType: entry.typ, Status: "active", Extra: string(encodedModel)}
			if err := tx.Create(&model).Error; err != nil {
				return err
			}
		}

		*entry.target = cfg.Name + "@" + instance.InstanceName + "@" + cfg.Factory
	}
	return tx.Model(tenant).Select("llm_id", "embd_id", "rerank_id", "asr_id", "img2txt_id").Updates(tenant).Error
}
