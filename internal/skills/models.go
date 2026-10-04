package skills

import (
	"context"
	"fmt"
	"strings"

	"gorm.io/gorm"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

// LegacyModels binds the exact Python tenant model row. It never resolves by name again.
type LegacyModels struct {
	DB        *gorm.DB
	Providers *entity.ProviderManager
}

func (m *LegacyModels) bind(ctx context.Context, tenant string, id int64, typ string) (entity.TenantLLM, models.ModelDriver, *models.APIConfig, error) {
	var row entity.TenantLLM
	e := m.DB.WithContext(ctx).Where("id = ? AND tenant_id = ? AND mdl_type = ? AND status = '1'", id, tenant, typ).First(&row).Error
	if e != nil {
		return row, nil, nil, missing(e)
	}
	if row.LLMName == nil || *row.LLMName == "" || m.Providers == nil {
		return row, nil, nil, fault(503, "MODEL_DRIVER_UNAVAILABLE")
	}
	if !driverSupports(row.LLMFactory, typ) {
		return row, nil, nil, fault(503, "MODEL_DRIVER_UNAVAILABLE")
	}
	p := m.Providers.FindProvider(row.LLMFactory)
	if p == nil && (strings.EqualFold(row.LLMFactory, "OpenAI-API-Compatible") || strings.EqualFold(row.LLMFactory, "vllm")) {
		p = &entity.Provider{Name: row.LLMFactory, URL: map[string]string{}, URLSuffix: models.URLSuffix{Embedding: "embeddings", Rerank: "rerank"}}
	}
	if p == nil {
		return row, nil, nil, fault(503, "MODEL_DRIVER_UNAVAILABLE")
	}
	urls := p.URL
	if row.APIBase != nil && *row.APIBase != "" {
		urls = map[string]string{"default": *row.APIBase}
	}
	driver, e := models.NewModelFactory().CreateModelDriver(p.Name, urls, p.URLSuffix)
	if e != nil {
		return row, nil, nil, fault(503, "MODEL_DRIVER_UNAVAILABLE")
	}
	if _, dummy := driver.(*models.DummyModel); dummy {
		return row, nil, nil, fault(503, "MODEL_DRIVER_UNAVAILABLE")
	}
	if strings.TrimSpace(urls["default"]) == "" {
		return row, nil, nil, fault(503, "MODEL_DRIVER_UNAVAILABLE")
	}
	return row, driver, &models.APIConfig{Context: ctx, APIKey: row.APIKey}, nil
}
func (m *LegacyModels) Validate(ctx context.Context, tenant string, id int64, typ string) error {
	_, _, _, e := m.bind(ctx, tenant, id, typ)
	return e
}
func (m *LegacyModels) Embed(ctx context.Context, tenant string, id int64, texts []string, query bool) ([][]float64, error) {
	row, driver, c, e := m.bind(ctx, tenant, id, "embedding")
	if e != nil {
		return nil, e
	}
	_ = query
	vectors, e := driver.Encode(row.LLMName, texts, c, nil)
	if e != nil {
		return nil, fault(503, "EMBEDDING_FAILED")
	}
	if _, _, e = vectors32(vectors, len(texts)); e != nil {
		return nil, e
	}
	return vectors, nil
}
func (m *LegacyModels) Rerank(ctx context.Context, tenant string, id int64, query string, texts []string) ([]float64, error) {
	row, _, _, e := m.bind(ctx, tenant, id, "rerank")
	if e != nil {
		return nil, e
	}
	base := ""
	if row.APIBase != nil {
		base = *row.APIBase
	}
	if base == "" {
		p := m.Providers.FindProvider(row.LLMFactory)
		if p != nil {
			base = p.URL["default"]
		}
	}
	key := ""
	if row.APIKey != nil {
		key = *row.APIKey
	}
	return strictRerank(ctx, base, key, *row.LLMName, query, texts)
}
func (m *LegacyModels) List(ctx context.Context, tenant string) ([]JSON, error) {
	var rows []entity.TenantLLM
	e := m.DB.WithContext(ctx).Where("tenant_id = ? AND status = '1' AND mdl_type IN ?", tenant, []string{"embedding", "rerank"}).Order("id").Find(&rows).Error
	if e != nil {
		return nil, e
	}
	out := []JSON{}
	for _, r := range rows {
		available := m.Validate(ctx, tenant, r.ID, *r.ModelType) == nil
		var reason any
		if !available {
			reason = "MODEL_DRIVER_UNAVAILABLE"
		}
		out = append(out, JSON{"id": fmt.Sprint(r.ID), "name": r.LLMName, "provider": r.LLMFactory, "type": r.ModelType, "max_tokens": r.MaxTokens, "available": available, "reason": reason})
	}
	return out, nil
}

func driverSupports(provider, typ string) bool {
	switch strings.ToLower(provider) {
	case "openai-api-compatible":
		return typ == "embedding" || typ == "rerank"
	case "vllm":
		return typ == "rerank"
	case "openai", "siliconflow":
		return typ == "embedding"
	case "zhipu-ai", "gitee":
		return typ == "embedding"
	}
	return false
}
