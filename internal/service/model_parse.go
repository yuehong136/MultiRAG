package service

import (
	"context"
	"fmt"
	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"strings"
	"time"
)

const documentParseType entity.ModelType = "doc_parse"

func (m *ModelProviderService) ParseFile(provider, instance, name, user string, content []byte, documentURL string, api *models.APIConfig) (*models.ParseTask, common.ErrorCode, error) {
	if err := models.ValidateParseInput(content, documentURL); err != nil {
		return nil, common.CodeBadRequest, err
	}
	driver, resolved, cancel, code, err := m.mediaModel(provider, instance, name, user, documentParseType, api)
	if err != nil {
		return nil, code, err
	}
	defer cancel()
	parser, ok := driver.(models.FileParser)
	if !ok {
		return nil, common.CodeNotImplemented, fmt.Errorf("%w: file parse", models.ErrUnsupportedCapability)
	}
	result, err := parser.ParseFile(&name, content, documentURL, resolved)
	if err == nil {
		err = mediaContext(resolved).Err()
	}
	if err == nil {
		err = models.ValidateParseTask(result, true)
	}
	if err != nil {
		code, err = mediaError(err)
		return nil, code, err
	}
	return result, common.CodeSuccess, nil
}

func (m *ModelProviderService) userTaskReader(provider, instance, user string, api *models.APIConfig) (models.TaskReader, *models.APIConfig, common.ErrorCode, error) {
	if strings.TrimSpace(provider) == "" || strings.TrimSpace(instance) == "" || strings.Contains(provider+instance, "@") {
		return nil, nil, common.CodeBadRequest, fmt.Errorf("invalid provider instance")
	}
	if m.providerManager == nil {
		return nil, nil, common.CodeServerError, fmt.Errorf("model providers are not initialized")
	}
	tenants, err := m.userTenantDAO.GetByUserIDAndRole(user, "owner")
	if err != nil {
		return nil, nil, common.CodeServerError, err
	}
	if len(tenants) == 0 {
		return nil, nil, common.CodeNotFound, fmt.Errorf("user has no tenants")
	}
	p, err := m.modelProviderDAO.GetByTenantIDAndProviderName(tenants[0].TenantID, provider)
	if err != nil {
		return nil, nil, common.CodeNotFound, fmt.Errorf("provider instance not found")
	}
	i, err := m.modelInstanceDAO.GetByProviderIDAndInstanceName(p.ID, instance)
	if err != nil {
		return nil, nil, common.CodeNotFound, fmt.Errorf("provider instance not found")
	}
	driver, resolved, err := instanceModelDriver(m.providerManager.FindProvider(provider), i, m.driverFactory)
	if err != nil {
		return nil, nil, common.CodeDataError, err
	}
	reader, ok := driver.(models.TaskReader)
	if !ok {
		return nil, nil, common.CodeNotImplemented, fmt.Errorf("%w: tasks", models.ErrUnsupportedCapability)
	}
	resolved.Context = mediaContext(api)
	return reader, resolved, common.CodeSuccess, nil
}

func (m *ModelProviderService) ListProviderTasks(provider, instance, user string, api *models.APIConfig) ([]models.ParseTask, common.ErrorCode, error) {
	ctx, cancel := context.WithTimeout(mediaContext(api), 120*time.Second)
	defer cancel()
	reader, resolved, code, err := m.userTaskReader(provider, instance, user, &models.APIConfig{Context: ctx})
	if err != nil {
		return nil, code, err
	}
	result, err := reader.ListTasks(resolved)
	if err == nil {
		err = ctx.Err()
	}
	if err == nil && result == nil {
		err = fmt.Errorf("missing task list")
	}
	for _, task := range result {
		if err != nil {
			break
		}
		err = models.ValidateParseTask(&task, false)
	}
	if err != nil {
		return nil, common.CodeServerError, err
	}
	return result, common.CodeSuccess, nil
}

func (m *ModelProviderService) ShowProviderTask(provider, instance, id, user string, api *models.APIConfig) (*models.ParseTask, common.ErrorCode, error) {
	if err := models.ValidateTaskID(id); err != nil {
		return nil, common.CodeBadRequest, err
	}
	ctx, cancel := context.WithTimeout(mediaContext(api), 120*time.Second)
	defer cancel()
	reader, resolved, code, err := m.userTaskReader(provider, instance, user, &models.APIConfig{Context: ctx})
	if err != nil {
		return nil, code, err
	}
	result, err := reader.ShowTask(id, resolved)
	if err == nil {
		err = ctx.Err()
	}
	if err == nil {
		err = models.ValidateParseTask(result, true)
	}
	if err == nil && result.TaskID != id {
		err = fmt.Errorf("task ID mismatch")
	}
	if err != nil {
		return nil, common.CodeServerError, err
	}
	return result, common.CodeSuccess, nil
}
