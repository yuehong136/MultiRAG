package service

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"regexp"
	"strconv"
	"strings"

	"gorm.io/gorm"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

var (
	ErrSearchbotArgument     = errors.New("invalid related questions request")
	ErrSearchbotAccessDenied = errors.New("search app is not accessible")
	searchbotQuestionRE      = regexp.MustCompile(`^\s*[0-9]+\.\s+(.+?)\s*$`)
	searchbotThinkRE         = regexp.MustCompile(`(?s)<think>.*?(?:</think>|$)`)
)

// SearchbotService generates suggestions using the authorized search tenant's model.
type SearchbotService struct {
	provider   *ModelProviderService
	loadSearch func(context.Context, string, string) (*entity.Search, error)
	loadTenant func(context.Context, string) (*entity.Tenant, error)
	loadLegacy func(context.Context, string, int64) (*entity.TenantLLM, error)
	bindModel  func(context.Context, string, any) (*models.ChatModel, error)
	loadPrompt func(string) (string, error)
}

func NewSearchbotService(provider *ModelProviderService) *SearchbotService {
	s := &SearchbotService{provider: provider, loadSearch: readableSearch, loadTenant: searchbotTenant, loadLegacy: searchbotLegacy, loadPrompt: LoadPrompt}
	s.bindModel = s.chatModel
	return s
}

// RelatedQuestions rejects invalid and inaccessible requests before model resolution.
func (s *SearchbotService) RelatedQuestions(ctx context.Context, userID, question, searchID string) ([]string, error) {
	if ctx == nil || strings.TrimSpace(userID) == "" || strings.TrimSpace(question) == "" || searchID != strings.TrimSpace(searchID) || len(searchID) > 128 {
		return nil, ErrSearchbotArgument
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	tenantID := userID
	config := entity.JSONMap{}
	if searchID != "" {
		search, err := s.loadSearch(ctx, userID, searchID)
		if err != nil {
			return nil, err
		}
		if search == nil || strings.TrimSpace(search.TenantID) == "" {
			return nil, ErrSearchbotAccessDenied
		}
		if search.SearchConfig == nil {
			return nil, ErrSearchbotArgument
		}
		tenantID, config = search.TenantID, search.SearchConfig
	}
	if value, exists := config["chat_id"]; exists && value == nil {
		return nil, ErrSearchbotArgument
	}
	selector, err := searchbotSelector(config["chat_id"])
	if err != nil {
		return nil, err
	}
	generation, err := searchbotGeneration(config)
	if err != nil {
		return nil, err
	}
	prompt, err := s.loadPrompt("related_question")
	if err != nil {
		return nil, err
	}
	model, err := s.bindModel(ctx, tenantID, selector)
	if err != nil {
		return nil, err
	}
	if model == nil || model.ModelDriver == nil || model.ModelName == nil {
		return nil, errors.New("related questions model is unavailable")
	}
	// Bind the context to a copy; callers may reuse a model without context races.
	local, api := *model, models.APIConfig{}
	if model.APIConfig != nil {
		api = *model.APIConfig
	}
	api.Context, local.APIConfig = ctx, &api
	response, err := local.ChatWithMessages([]models.Message{
		{Role: "system", Content: prompt},
		{Role: "user", Content: "Keywords: " + question + "\nRelated search terms:\n"},
	}, generation)
	if err != nil {
		return nil, err
	}
	return parseSearchbotQuestions(*response.Answer), nil
}

// readableSearch never reads an inaccessible search configuration into the caller.
func readableSearch(ctx context.Context, userID, searchID string) (*entity.Search, error) {
	if ctx == nil || strings.TrimSpace(userID) == "" || searchID == "" || searchID != strings.TrimSpace(searchID) || len(searchID) > 128 {
		return nil, ErrSearchbotArgument
	}
	if dao.DB == nil {
		return nil, errors.New("search database is unavailable")
	}
	db := dao.DB.WithContext(ctx)
	memberships := db.Model(&entity.UserTenant{}).Select("tenant_id").
		Where("user_id = ? AND status = ? AND role IN ?", userID, "1", []string{"owner", "admin", "normal"})
	var search entity.Search
	err := db.Where("id = ? AND status = ?", searchID, "1").
		Where("tenant_id IN (?)", memberships).
		Where("EXISTS (SELECT 1 FROM t_ai_users AS owner_user WHERE owner_user.id = t_ai_search.tenant_id AND owner_user.status = ?)", "1").
		Where("EXISTS (SELECT 1 FROM t_ai_tenants AS owner_tenant WHERE owner_tenant.id = t_ai_search.tenant_id AND owner_tenant.status = ?)", "1").First(&search).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, ErrSearchbotAccessDenied
	}
	if err != nil {
		return nil, err
	}
	return &search, nil
}

func searchbotTenant(ctx context.Context, tenantID string) (*entity.Tenant, error) {
	if dao.DB == nil {
		return nil, errors.New("model database is unavailable")
	}
	var tenant entity.Tenant
	err := dao.DB.WithContext(ctx).Where("id = ? AND status = ?", tenantID, "1").
		Where("EXISTS (SELECT 1 FROM t_ai_users AS owner_user WHERE owner_user.id = t_ai_tenants.id AND owner_user.status = ?)", "1").First(&tenant).Error
	return &tenant, err
}

func searchbotLegacy(ctx context.Context, tenantID string, id int64) (*entity.TenantLLM, error) {
	if dao.DB == nil {
		return nil, errors.New("model database is unavailable")
	}
	var model entity.TenantLLM
	err := dao.DB.WithContext(ctx).Where("id = ? AND tenant_id = ? AND status = ?", id, tenantID, "1").First(&model).Error
	return &model, err
}

// chatModel preserves positive numeric legacy identities rather than rebinding by name.
func (s *SearchbotService) chatModel(ctx context.Context, tenantID string, selector any) (*models.ChatModel, error) {
	if ctx == nil || strings.TrimSpace(tenantID) == "" {
		return nil, ErrSearchbotArgument
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	reference, err := searchbotSelector(selector)
	if err != nil {
		return nil, err
	}
	// Every selector, including an explicit legacy ID, requires a live owner.
	tenant, err := s.loadTenant(ctx, tenantID)
	if err != nil {
		return nil, err
	}
	if tenant == nil || tenant.ID != tenantID || tenant.Status == nil || *tenant.Status != "1" {
		return nil, errors.New("chat model owner is unavailable")
	}
	if reference == "" {
		if tenant.TenantLLMID != nil {
			reference, err = searchbotSelector(strconv.FormatInt(*tenant.TenantLLMID, 10))
		} else {
			reference, err = searchbotSelector(tenant.LLMID)
		}
		if err != nil || reference == "" {
			return nil, errors.New("default chat model is unavailable")
		}
	}
	if s.provider == nil {
		return nil, errors.New("model providers are unavailable")
	}
	if id, err := strconv.ParseInt(reference, 10, 64); err == nil {
		row, err := s.loadLegacy(ctx, tenantID, id)
		if err != nil {
			return nil, errors.New("selected chat model is unavailable")
		}
		return s.legacyChatModel(ctx, tenantID, id, row)
	}
	model, err := s.provider.GetChatModel(tenantID, reference)
	if err != nil {
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return model, nil
}

func (s *SearchbotService) legacyChatModel(ctx context.Context, tenantID string, id int64, row *entity.TenantLLM) (*models.ChatModel, error) {
	if id <= 0 || row == nil || row.ID != id || row.TenantID != tenantID || row.Status != "1" || row.ModelType == nil || !modelTypeMatches(*row.ModelType, entity.ModelTypeChat) || row.LLMName == nil || strings.TrimSpace(*row.LLMName) == "" {
		return nil, errors.New("selected chat model is unavailable")
	}
	if s.provider == nil || s.provider.providerManager == nil {
		return nil, errors.New("model providers are unavailable")
	}
	provider := s.provider.providerManager.FindProvider(row.LLMFactory)
	if provider == nil && strings.EqualFold(row.LLMFactory, "GiteeAI") {
		provider = s.provider.providerManager.FindProvider("Gitee")
	}
	if provider == nil && strings.EqualFold(row.LLMFactory, "OpenAI-API-Compatible") {
		provider = &entity.Provider{Name: row.LLMFactory, URLSuffix: models.URLSuffix{Chat: "chat/completions"}}
	}
	if provider == nil || row.APIKey == nil || strings.TrimSpace(*row.APIKey) == "" || row.MaxTokens < 0 {
		return nil, errors.New("selected chat model configuration is unavailable")
	}
	urls := make(map[string]string, len(provider.URL))
	for region, endpoint := range provider.URL {
		urls[region] = endpoint
	}
	if row.APIBase != nil && strings.TrimSpace(*row.APIBase) != "" {
		urls = map[string]string{"default": *row.APIBase}
	}
	if strings.TrimSpace(urls["default"]) == "" && !strings.EqualFold(provider.Name, "Bedrock") {
		return nil, errors.New("selected chat model endpoint is unavailable")
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	var factory models.DriverFactory = models.NewModelFactory()
	if s.provider.driverFactory != nil {
		factory = s.provider.driverFactory
	}
	driver, err := factory.CreateModelDriver(provider.Name, urls, provider.URLSuffix)
	if err != nil {
		return nil, err
	}
	name, key := *row.LLMName, *row.APIKey
	bound := models.NewChatModel(driver, &name, &models.APIConfig{APIKey: &key, Context: ctx})
	maxTokens := int(row.MaxTokens)
	bound.ModelConfig.MaxTokens = &maxTokens
	if definition, err := s.provider.providerManager.GetModelByName(provider.Name, name); err == nil {
		applyModelChatDefaults(definition, &bound.ModelConfig)
	}
	return bound, nil
}

func searchbotSelector(value any) (string, error) {
	if value == nil {
		return "", nil
	}
	var reference string
	switch v := value.(type) {
	case string:
		reference = v
	case json.Number:
		reference = string(v)
	case int:
		reference = strconv.Itoa(v)
	case int64:
		reference = strconv.FormatInt(v, 10)
	case float64:
		if math.IsNaN(v) || math.IsInf(v, 0) || math.Trunc(v) != v || v >= 1<<53 {
			return "", ErrSearchbotArgument
		}
		reference = strconv.FormatFloat(v, 'f', -1, 64)
	default:
		return "", ErrSearchbotArgument
	}
	if reference == "" {
		return "", nil
	}
	if reference != strings.TrimSpace(reference) || len(reference) > 512 {
		return "", ErrSearchbotArgument
	}
	if id, err := strconv.ParseInt(reference, 10, 64); err == nil {
		if id <= 0 {
			return "", ErrSearchbotArgument
		}
		return strconv.FormatInt(id, 10), nil
	}
	if _, _, _, err := splitModelInstance(reference); err != nil {
		return "", ErrSearchbotArgument
	}
	return reference, nil
}

func searchbotGeneration(config entity.JSONMap) (map[string]interface{}, error) {
	raw, exists := config["llm_setting"]
	if !exists {
		return map[string]interface{}{"temperature": 0.9}, nil
	}
	settings, ok := raw.(map[string]interface{})
	if !ok {
		if typed, valid := raw.(entity.JSONMap); valid {
			settings, ok = map[string]interface{}(typed), true
		}
	}
	if !ok {
		return nil, ErrSearchbotArgument
	}
	data, err := json.Marshal(settings)
	if err != nil {
		return nil, ErrSearchbotArgument
	}
	var parsed struct {
		Temperature *float64  `json:"temperature"`
		TopP        *float64  `json:"top_p"`
		MaxTokens   *int      `json:"max_tokens"`
		Vision      *bool     `json:"vision"`
		Thinking    *bool     `json:"thinking"`
		Stream      *bool     `json:"stream"`
		DoSample    *bool     `json:"do_sample"`
		Stop        *[]string `json:"stop"`
		Effort      *string   `json:"reasoning_effort"`
		EffortAlias *string   `json:"effort"`
		Verbosity   *string   `json:"verbosity"`
	}
	if err := json.Unmarshal(data, &parsed); err != nil || parsed.Temperature != nil && (*parsed.Temperature < 0 || *parsed.Temperature > 2) || parsed.TopP != nil && (*parsed.TopP < 0 || *parsed.TopP > 1) || parsed.MaxTokens != nil && *parsed.MaxTokens < 0 {
		return nil, ErrSearchbotArgument
	}
	result := make(map[string]interface{}, len(settings))
	for key, value := range settings {
		if key != "parameter" {
			result[key] = value
		}
	}
	return result, nil
}

func parseSearchbotQuestions(answer string) []string {
	questions := make([]string, 0)
	for _, line := range strings.Split(searchbotThinkRE.ReplaceAllString(answer, ""), "\n") {
		if match := searchbotQuestionRE.FindStringSubmatch(line); len(match) == 2 {
			if question := strings.TrimSpace(match[1]); question != "" {
				questions = append(questions, question)
			}
		}
	}
	return questions
}
