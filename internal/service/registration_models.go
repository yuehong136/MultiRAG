package service

import (
	"encoding/json"
	"fmt"
	"gorm.io/gorm"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"strings"
)

// initializeTenantModels uses only the configured defaults and their own credentials.
// All rows and default references participate in the registration transaction.
func initializeTenantModels(tx *gorm.DB, tenant *entity.Tenant, models server.DefaultModelsConfig) error {
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
		if !supported || definition.MaxTokens <= 0 {
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

		model := entity.TenantModel{ID: common.GenerateUUID(), ProviderID: providerID, InstanceID: instance.ID, ModelName: cfg.Name, ModelType: entry.typ, Status: "active", Extra: string(encodedModel)}
		if err := tx.Create(&model).Error; err != nil {
			return err
		}
		*entry.target = cfg.Name + "@" + instance.InstanceName + "@" + cfg.Factory
	}
	return tx.Model(tenant).Select("llm_id", "embd_id", "rerank_id", "asr_id", "img2txt_id").Updates(tenant).Error
}
