package service

import (
	"errors"
	"fmt"
	"sort"
	"strings"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
)

var errCatalogModelDeletion = errors.New("catalog models must be disabled, not deleted")

func deletionNames(names []string) ([]string, error) {
	if len(names) == 0 {
		return nil, fmt.Errorf("at least one name is required")
	}
	unique := make(map[string]bool, len(names))
	for _, name := range names {
		if strings.TrimSpace(name) == "" {
			return nil, fmt.Errorf("empty names are not allowed")
		}
		unique[name] = true
	}
	result := make([]string, 0, len(unique))
	for name := range unique {
		result = append(result, name)
	}
	sort.Strings(result)
	return result, nil
}

// Lock the owned provider first, then its instances in name order. Creation and
// deletion use the same locks so concurrent declarations cannot leave orphans.
func (m *ModelProviderService) mutateOwnedModelResources(providerName, userID string, operation func(*gorm.DB, *entity.TenantModelProvider) error) (common.ErrorCode, error) {
	if strings.TrimSpace(providerName) == "" || strings.TrimSpace(userID) == "" {
		return common.CodeBadRequest, fmt.Errorf("provider and user are required")
	}
	tenants, err := m.userTenantDAO.GetByUserIDAndRole(userID, "owner")
	if err != nil {
		return common.CodeServerError, err
	}
	if len(tenants) == 0 {
		return common.CodeNotFound, fmt.Errorf("user has no tenants")
	}
	err = dao.DB.Transaction(func(tx *gorm.DB) error {
		var provider entity.TenantModelProvider
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("tenant_id = ? AND provider_name = ?", tenants[0].TenantID, providerName).First(&provider).Error; err != nil {
			return err
		}
		return operation(tx, &provider)
	})
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return common.CodeNotFound, fmt.Errorf("provider, instance or model not found")
	}
	if errors.Is(err, errCatalogModelDeletion) {
		return common.CodeBadRequest, err
	}
	if err != nil {
		return common.CodeServerError, err
	}
	return common.CodeSuccess, nil
}

func (m *ModelProviderService) DropProviderInstances(providerName, userID string, instances []string) (common.ErrorCode, error) {
	names, err := deletionNames(instances)
	if err != nil {
		return common.CodeBadRequest, err
	}
	return m.mutateOwnedModelResources(providerName, userID, func(tx *gorm.DB, provider *entity.TenantModelProvider) error {
		for _, name := range names {
			var instance entity.TenantModelInstance
			if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("provider_id = ? AND instance_name = ?", provider.ID, name).First(&instance).Error; err != nil {
				return err
			}
			if err := tx.Unscoped().Where("provider_id = ? AND instance_id = ?", provider.ID, instance.ID).Delete(&entity.TenantModel{}).Error; err != nil {
				return err
			}
			result := tx.Unscoped().Where("provider_id = ? AND id = ?", provider.ID, instance.ID).Delete(&entity.TenantModelInstance{})
			if result.Error != nil {
				return result.Error
			}
			if result.RowsAffected != 1 {
				return gorm.ErrRecordNotFound
			}
		}
		return nil
	})
}

func (m *ModelProviderService) DropInstanceModels(providerName, instanceName, userID string, modelNames []string) (common.ErrorCode, error) {
	names, err := deletionNames(modelNames)
	if err != nil {
		return common.CodeBadRequest, err
	}
	if strings.TrimSpace(instanceName) == "" {
		return common.CodeBadRequest, fmt.Errorf("instance name is required")
	}
	return m.mutateOwnedModelResources(providerName, userID, func(tx *gorm.DB, provider *entity.TenantModelProvider) error {
		var instance entity.TenantModelInstance
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("provider_id = ? AND instance_name = ?", provider.ID, instanceName).First(&instance).Error; err != nil {
			return err
		}
		for _, name := range names {
			if _, err := m.providerManager.GetModelByName(providerName, name); err == nil {
				return errCatalogModelDeletion
			}
			result := tx.Unscoped().Where("provider_id = ? AND instance_id = ? AND model_name = ?", provider.ID, instance.ID, name).Delete(&entity.TenantModel{})
			if result.Error != nil {
				return result.Error
			}
			if result.RowsAffected == 0 {
				return gorm.ErrRecordNotFound
			}
		}
		return nil
	})
}
