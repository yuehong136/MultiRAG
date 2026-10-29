package service

import (
	"context"
	"fmt"

	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

// DifyModelBindings resolves the authorized dataset owner's stored selections.
// An explicit numeric reference is authoritative and cannot fall back by name.
type DifyModelBindings struct{ provider *ModelProviderService }

func NewDifyModelBindings(provider *ModelProviderService) *DifyModelBindings {
	return &DifyModelBindings{provider: provider}
}

func (b *DifyModelBindings) Embedding(ctx context.Context, kb *entity.Knowledgebase) (*models.EmbeddingModel, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if b == nil || b.provider == nil || kb == nil || kb.TenantID == "" || dao.DB == nil {
		return nil, fmt.Errorf("embedding binding is unavailable")
	}
	if kb.TenantEmbdID != nil {
		return b.provider.GetEmbeddingModelByIDWithContext(ctx, kb.TenantID, *kb.TenantEmbdID)
	}
	selector, err := retrievalModelSelector(ctx, kb.TenantID, entity.ModelTypeEmbedding, kb.TenantEmbdID, kb.EmbdID)
	if err != nil {
		return nil, err
	}
	model, err := b.provider.GetEmbeddingModelWithContext(ctx, kb.TenantID, selector)
	if err != nil {
		return nil, err
	}
	return model, ctx.Err()
}

func (b *DifyModelBindings) Chat(ctx context.Context, kb *entity.Knowledgebase) (*models.ChatModel, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if b == nil || b.provider == nil || kb == nil || kb.TenantID == "" || dao.DB == nil {
		return nil, fmt.Errorf("chat binding is unavailable")
	}
	var tenant entity.Tenant
	if err := dao.DB.WithContext(ctx).Where("id = ? AND status = ?", kb.TenantID, string(entity.StatusValid)).First(&tenant).Error; err != nil {
		return nil, err
	}
	if tenant.TenantLLMID != nil {
		return b.provider.GetChatModelByIDWithContext(ctx, kb.TenantID, *tenant.TenantLLMID)
	}
	selector, err := retrievalModelSelector(ctx, kb.TenantID, entity.ModelTypeChat, tenant.TenantLLMID, tenant.LLMID)
	if err != nil {
		return nil, err
	}
	model, err := b.provider.GetChatModelWithContext(ctx, kb.TenantID, selector)
	if err != nil {
		return nil, err
	}
	return model, ctx.Err()
}
