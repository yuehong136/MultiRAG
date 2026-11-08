package service

import (
	"context"
	"fmt"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"slices"
	"sort"
	"strings"
)

// TenantModelItem is a credential-free numeric binding inventory.
type TenantModelItem struct {
	ID       int64  `json:"id"`
	Provider string `json:"provider_name"`
	Name     string `json:"model_name"`
	Type     string `json:"model_type"`
}

func (m *ModelProviderService) ListTenantModels(ctx context.Context, user string, filters ...string) ([]map[string]any, error) {
	tenant, err := activeOwnedModelTenant(ctx, user)
	if err != nil {
		return nil, err
	}
	filter := ""
	if len(filters) > 0 && filters[0] != "" {
		typ, err := normalizeCustomModelType(filters[0])
		if err != nil {
			return nil, err
		}
		filter = string(typ)
	}
	bindings := []TenantModelItem{}
	err = dao.DB.WithContext(ctx).Model(&entity.TenantLLM{}).Select("id, llm_factory AS provider, llm_name AS name, mdl_type AS type").Where("tenant_id = ? AND status = ? AND mdl_type IS NOT NULL AND llm_name IS NOT NULL", tenant, "1").Order("id").Scan(&bindings).Error
	if err != nil {
		return nil, err
	}
	rows := []map[string]any{}
	for _, b := range bindings {
		if filter != "" && !modelTypeMatches(b.Type, entity.ModelType(filter)) {
			continue
		}
		rows = append(rows, map[string]any{"id": b.ID, "model_id": b.ID, "provider_name": b.Provider, "llm_factory": b.Provider, "model_name": b.Name, "llm_name": b.Name, "model_type": []string{b.Type}, "model_types": []string{b.Type}})
	}
	providers, err := m.modelProviderDAO.GetByTenantIDWithContext(ctx, tenant)
	if err != nil {
		return nil, err
	}
	ids := []string{}
	byProvider := map[string]*entity.TenantModelProvider{}
	for _, p := range providers {
		ids = append(ids, p.ID)
		byProvider[p.ID] = p
	}
	instances, err := m.modelInstanceDAO.GetByProviderIDsWithContext(ctx, ids)
	if err != nil {
		return nil, err
	}
	instanceIDs := []string{}
	for _, instance := range instances {
		instanceIDs = append(instanceIDs, instance.ID)
	}
	overrides, err := m.modelDAO.GetModelsByProviderIDsAndInstanceIDsWithContext(ctx, ids, instanceIDs)
	if err != nil {
		return nil, err
	}
	byInstance := map[string][]*entity.TenantModel{}
	for _, override := range overrides {
		byInstance[override.InstanceID] = append(byInstance[override.InstanceID], override)
	}
	for _, instance := range instances {
		provider := byProvider[instance.ProviderID]
		if provider == nil {
			return nil, fmt.Errorf("model instance provider unavailable")
		}
		if m.providerManager == nil {
			return nil, fmt.Errorf("provider catalog unavailable")
		}
		definition := m.providerManager.FindProvider(provider.ProviderName)
		if definition == nil {
			continue
		}
		models := map[string][]string{}
		for _, catalog := range definition.Models {
			models[catalog.Name] = slices.Clone(catalog.ModelTypes)
		}
		for _, stored := range byInstance[instance.ID] {
			if stored.ProviderID != instance.ProviderID {
				return nil, fmt.Errorf("model binding provider mismatch")
			}
			if stored.Status != "active" || stored.Extra == "" || stored.Extra == "{}" {
				delete(models, stored.ModelName)
				continue
			}
			extra, err := decodeCustomModelExtra(stored.Extra)
			if err != nil {
				return nil, err
			}
			types, err := normalizeCustomModelTypes(stored.ModelType, extra.ModelTypes)
			if err != nil {
				return nil, err
			}
			models[stored.ModelName] = types
		}
		names := make([]string, 0, len(models))
		for name := range models {
			names = append(names, name)
		}
		sort.Strings(names)
		for _, name := range names {
			types := models[name]
			if filter != "" && !slices.Contains(types, filter) {
				continue
			}
			rows = append(rows, map[string]any{"provider_name": provider.ProviderName, "llm_factory": provider.ProviderName, "instance_name": instance.InstanceName, "model_name": name, "llm_name": name, "model_type": types, "model_types": types})
		}
	}
	sort.SliceStable(rows, func(i, j int) bool {
		key := func(row map[string]any) string {
			return strings.ToLower(fmt.Sprint(row["provider_name"])) + "/" + fmt.Sprint(row["instance_name"]) + "/" + fmt.Sprint(row["model_name"])
		}
		return key(rows[i]) < key(rows[j])
	})
	return rows, nil
}
func (s *TenantService) SetTenantDefaultModelByID(ctx context.Context, user, kind string, id int64) error {
	columns := map[string]string{"chat": "tenant_llm_id", "embedding": "tenant_embd_id", "rerank": "tenant_rerank_id", "asr": "tenant_asr_id", "image2text": "tenant_img2txt_id", "tts": "tenant_tts_id"}
	column, ok := columns[kind]
	if !ok || id <= 0 {
		return fmt.Errorf("positive binding ID and supported model_type required")
	}
	return dao.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var owner entity.UserTenant
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("user_id = ? AND role = ? AND status = ?", user, "owner", "1").First(&owner).Error; err != nil {
			return err
		}
		var tenant entity.Tenant
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ? AND status = ?", owner.TenantID, "1").First(&tenant).Error; err != nil {
			return err
		}
		var binding entity.TenantLLM
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ? AND tenant_id = ? AND status = ?", id, owner.TenantID, "1").First(&binding).Error; err != nil {
			return err
		}
		if binding.ModelType == nil || !modelTypeMatches(*binding.ModelType, entity.ModelType(kind)) || binding.LLMName == nil || *binding.LLMName == "" {
			return fmt.Errorf("typed model binding unavailable")
		}
		return tx.Model(&tenant).Update(column, id).Error
	})
}

// ListTenantDefaultModelsWithContext includes numeric defaults without credentials.
func (s *TenantService) ListTenantDefaultModelsWithContext(ctx context.Context, user string) ([]ModelItem, error) {
	tenantID, err := activeOwnedModelTenant(ctx, user)
	if err != nil {
		return nil, err
	}
	var tenant entity.Tenant
	if err := dao.DB.WithContext(ctx).Where("id = ?", tenantID).First(&tenant).Error; err != nil {
		return nil, err
	}
	legacy, err := s.ListTenantDefaultModels(user)
	if err != nil {
		return nil, err
	}
	result := make([]ModelItem, 0, 6)
	for _, kind := range []string{"chat", "embedding", "rerank", "asr", "image2text", "tts"} {
		id := tenantDefaultBindingID(&tenant, entity.ModelType(kind))
		responseKind := kind
		if kind == "chat" {
			responseKind = "llm"
		}
		if id == nil {
			for _, item := range legacy {
				if item.ModelType == responseKind {
					result = append(result, item)
				}
			}
			continue
		}
		item := ModelItem{ModelID: id, ModelType: responseKind}
		var row entity.TenantLLM
		err := dao.DB.WithContext(ctx).Where("id = ? AND tenant_id = ?", *id, tenantID).First(&row).Error
		if err != nil {
			if err == gorm.ErrRecordNotFound {
				result = append(result, item)
				continue
			}
			return nil, err
		}
		item.ModelProvider = &row.LLMFactory
		item.ModelName = row.LLMName
		item.Enable = row.Status == "1" && row.ModelType != nil && modelTypeMatches(*row.ModelType, entity.ModelType(kind))
		result = append(result, item)
	}
	return result, nil
}

func providerAllowsEmptyModelKey(name string) bool {
	switch strings.ToLower(name) {
	case "vllm", "ollama", "lm-studio", "localai", "xinference", "mineru_local", "paddleocr_local", "modelscope":
		return true
	}
	return false
}

func isExcludedTenantProvider(name string) bool {
	switch strings.ToLower(name) {
	case "youdao", "fastembed", "baai", "builtin", "siliconflow_intl":
		return true
	}
	return false
}
