package service

import (
	"context"
	"fmt"
	"math"
	"strconv"
	"strings"

	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

// SkillModelProvider resolves a complete legacy reference or decimal row ID.
// It deliberately never substitutes a similarly named provider instance.
type SkillModelProvider struct{}
type skillEmbeddingModel struct {
	*models.EmbeddingModel
	MaxTokens int
}

func NewSkillModelProvider() *SkillModelProvider { return &SkillModelProvider{} }
func (p *SkillModelProvider) GetEmbeddingModel(tenant, ref string) (*skillEmbeddingModel, error) {
	if strings.Count(ref, "@") == 2 {
		// The existing model service resolves the exact named provider instance;
		// never strip the instance component or fall back to a same-name legacy row.
		bound, err := NewModelProviderService().GetEmbeddingModel(tenant, ref)
		if err != nil {
			return nil, err
		}
		if _, dummy := bound.ModelDriver.(*models.DummyModel); dummy {
			return nil, fmt.Errorf("MODEL_DRIVER_UNAVAILABLE")
		}
		parts := strings.Split(ref, "@")
		switch strings.ToLower(parts[2]) {
		case "openai-api-compatible", "openai", "siliconflow", "zhipu-ai", "gitee":
		default:
			return nil, fmt.Errorf("MODEL_DRIVER_UNAVAILABLE")
		}
		return &skillEmbeddingModel{EmbeddingModel: bound, MaxTokens: defaultMaxLength}, nil
	}
	var rows []entity.TenantLLM
	q := dao.DB.Where("tenant_id = ? AND mdl_type = 'embedding' AND status = '1'", tenant)
	if id, err := strconv.ParseInt(ref, 10, 64); err == nil {
		q = q.Where("id = ?", id)
	} else {
		parts := strings.Split(ref, "@")
		if len(parts) != 2 {
			return nil, fmt.Errorf("MODEL_REFERENCE_INVALID")
		}
		q = q.Where("llm_name = ? AND llm_factory = ?", parts[0], parts[1])
	}
	if err := q.Find(&rows).Error; err != nil {
		return nil, err
	}
	if len(rows) != 1 {
		return nil, fmt.Errorf("MODEL_REFERENCE_UNRESOLVED")
	}
	row := rows[0]
	manager := dao.GetModelProviderManager()
	var provider *entity.Provider
	if manager != nil {
		provider = manager.FindProvider(row.LLMFactory)
	}
	if provider == nil && strings.EqualFold(row.LLMFactory, "OpenAI-API-Compatible") {
		provider = &entity.Provider{Name: row.LLMFactory, URL: map[string]string{}, URLSuffix: models.URLSuffix{Embedding: "embeddings"}}
	}
	if provider == nil {
		return nil, fmt.Errorf("MODEL_DRIVER_UNAVAILABLE")
	}
	// These drivers use symmetric embeddings; query and document requests share
	// the same encoding. Asymmetric providers are disabled until their query
	// input_type contract is implemented and verified.
	switch strings.ToLower(row.LLMFactory) {
	case "openai-api-compatible", "openai", "siliconflow", "zhipu-ai", "gitee":
	default:
		return nil, fmt.Errorf("MODEL_DRIVER_UNAVAILABLE")
	}
	urls := provider.URL
	if row.APIBase != nil && *row.APIBase != "" {
		urls = map[string]string{"default": *row.APIBase}
	}
	driver, err := models.NewModelFactory().CreateModelDriver(provider.Name, urls, provider.URLSuffix)
	if err != nil {
		return nil, err
	}
	maxTokens := defaultMaxLength
	if row.MaxTokens > 0 {
		maxTokens = int(row.MaxTokens)
	}
	return &skillEmbeddingModel{EmbeddingModel: models.NewEmbeddingModel(driver, row.LLMName, &models.APIConfig{APIKey: row.APIKey}), MaxTokens: maxTokens}, nil
}
func (m *skillEmbeddingModel) Encode(ctx context.Context, texts []string, query bool) ([][]float64, error) {
	m.APIConfig.Context = ctx
	m.EmbeddingConfig = &models.EmbeddingConfig{Query: query}
	vectors, err := m.EmbeddingModel.Encode(texts)
	if err != nil {
		return nil, err
	}
	for _, vector := range vectors {
		norm := 0.0
		for _, v := range vector {
			norm += v * v
		}
		if len(vector) > 32768 || norm == 0 || math.IsInf(norm, 0) {
			return nil, fmt.Errorf("EMBEDDING_INVALID")
		}
	}
	return vectors, nil
}
