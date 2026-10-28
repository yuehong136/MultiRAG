package service

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"reflect"
	"testing"

	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

type searchbotTestDriver struct {
	models.ModelDriver
	chat func(string, *models.APIConfig, []models.Message, *models.ChatConfig) (*models.ChatResponse, error)
}

func (d *searchbotTestDriver) ChatWithMessages(name string, api *models.APIConfig, messages []models.Message, config *models.ChatConfig) (*models.ChatResponse, error) {
	return d.chat(name, api, messages, config)
}

type searchbotTestFactory struct {
	driver models.ModelDriver
	calls  int
	name   string
	urls   map[string]string
}

func (f *searchbotTestFactory) CreateModelDriver(name string, urls map[string]string, _ models.URLSuffix) (models.ModelDriver, error) {
	f.calls++
	f.name, f.urls = name, urls
	return f.driver, nil
}

func TestSearchbotSelector(t *testing.T) {
	for _, test := range []struct {
		input any
		want  string
	}{
		{nil, ""}, {"", ""}, {"42", "42"}, {float64(42), "42"}, {json.Number("42"), "42"}, {int64(42), "42"},
		{"model@instance@OpenAI", "model@instance@OpenAI"}, {"model@OpenAI", "model@OpenAI"},
	} {
		got, err := searchbotSelector(test.input)
		if err != nil || got != test.want {
			t.Errorf("selector(%v) = %q, %v", test.input, got, err)
		}
	}
	for _, input := range []any{"0", "-1", " 42", "model", true, []any{}, 0.0, 1.5, math.Inf(1), math.NaN(), float64(1 << 53), json.Number("1.5")} {
		if _, err := searchbotSelector(input); !errors.Is(err, ErrSearchbotArgument) {
			t.Errorf("invalid selector %v accepted: %v", input, err)
		}
	}
}

func TestSearchbotGenerationRejectsBeforeModel(t *testing.T) {
	for _, setting := range []any{nil, false, []any{}, map[string]any{"temperature": -0.1}, map[string]any{"temperature": "0"}, map[string]any{"top_p": 2}, map[string]any{"max_tokens": -1}, map[string]any{"thinking": "false"}, map[string]any{"stop": 0}, map[string]any{"temperature": math.NaN()}} {
		s := NewSearchbotService(nil)
		s.loadSearch = func(context.Context, string, string) (*entity.Search, error) {
			return &entity.Search{TenantID: "owner", SearchConfig: entity.JSONMap{"llm_setting": setting}}, nil
		}
		calls := 0
		s.bindModel = func(context.Context, string, any) (*models.ChatModel, error) { calls++; return nil, nil }
		if _, err := s.RelatedQuestions(context.Background(), "member", "question", "search"); !errors.Is(err, ErrSearchbotArgument) || calls != 0 {
			t.Fatalf("invalid setting %v: err=%v model calls=%d", setting, err, calls)
		}
	}
}

func TestSearchbotExplicitNullModelDoesNotUseDefault(t *testing.T) {
	for _, config := range []entity.JSONMap{nil, {"chat_id": nil}} {
		s := NewSearchbotService(nil)
		s.loadSearch = func(context.Context, string, string) (*entity.Search, error) {
			return &entity.Search{TenantID: "owner", SearchConfig: config}, nil
		}
		calls := 0
		s.bindModel = func(context.Context, string, any) (*models.ChatModel, error) {
			calls++
			return nil, nil
		}
		if _, err := s.RelatedQuestions(context.Background(), "member", "question", "search"); !errors.Is(err, ErrSearchbotArgument) || calls != 0 {
			t.Fatalf("null search configuration used default model: error=%v calls=%d", err, calls)
		}
	}
}

func TestSearchbotRelatedQuestionsTenantAndZeroConfig(t *testing.T) {
	ctx := context.WithValue(context.Background(), struct{}{}, "request")
	s := NewSearchbotService(nil)
	settings := map[string]any{"temperature": 0.0, "top_p": 0.0, "max_tokens": 0, "thinking": false, "parameter": true}
	s.loadSearch = func(got context.Context, user, id string) (*entity.Search, error) {
		if got != ctx || user != "member" || id != "search" {
			t.Fatal("wrong authorization request")
		}
		return &entity.Search{TenantID: "owner", SearchConfig: entity.JSONMap{"chat_id": float64(42), "llm_setting": settings}}, nil
	}
	s.loadPrompt = func(name string) (string, error) {
		if name != "related_question" {
			t.Fatal(name)
		}
		return "shared prompt", nil
	}
	driver := &searchbotTestDriver{chat: func(name string, api *models.APIConfig, messages []models.Message, cfg *models.ChatConfig) (*models.ChatResponse, error) {
		if name != "selected" || api.Context != ctx || cfg.Temperature == nil || *cfg.Temperature != 0 || cfg.TopP == nil || *cfg.TopP != 0 || cfg.MaxTokens == nil || *cfg.MaxTokens != 0 || cfg.Thinking == nil || *cfg.Thinking {
			t.Fatal("tenant/model/request context or explicit zero configuration lost")
		}
		if len(messages) != 2 || messages[0].Content != "shared prompt" || messages[1].Content != "Keywords: question\nRelated search terms:\n" {
			t.Fatal("wrong messages")
		}
		answer := "<think>\n1. private reasoning\n</think>\n1. first\n10. tenth\nno list"
		return &models.ChatResponse{Answer: &answer}, nil
	}}
	name := "selected"
	originalAPI := &models.APIConfig{Context: context.Background()}
	bound := models.NewChatModel(driver, &name, originalAPI)
	s.bindModel = func(_ context.Context, tenant string, selector any) (*models.ChatModel, error) {
		if tenant != "owner" || selector != "42" {
			t.Fatal("authorized tenant or numeric selector lost")
		}
		return bound, nil
	}
	got, err := s.RelatedQuestions(ctx, "member", "question", "search")
	if err != nil || !reflect.DeepEqual(got, []string{"first", "tenth"}) {
		t.Fatalf("questions=%v error=%v", got, err)
	}
	if bound.APIConfig != originalAPI || bound.APIConfig.Context == ctx || settings["parameter"] != true {
		t.Fatal("shared binding or config mutated")
	}
}

func TestSearchbotRejectsWithoutModel(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	for _, test := range []struct {
		ctx                    context.Context
		user, question, search string
		want                   error
	}{
		{context.Background(), "user", " ", "search", ErrSearchbotArgument},
		{context.Background(), "", "question", "search", ErrSearchbotArgument},
		{context.Background(), "user", "question", " search", ErrSearchbotArgument},
		{ctx, "user", "question", "search", context.Canceled},
		{context.Background(), "user", "question", "foreign", ErrSearchbotAccessDenied},
	} {
		s := NewSearchbotService(nil)
		modelCalls := 0
		s.loadSearch = func(context.Context, string, string) (*entity.Search, error) { return nil, ErrSearchbotAccessDenied }
		s.bindModel = func(context.Context, string, any) (*models.ChatModel, error) { modelCalls++; return nil, nil }
		if _, err := s.RelatedQuestions(test.ctx, test.user, test.question, test.search); !errors.Is(err, test.want) || modelCalls != 0 {
			t.Fatalf("err=%v calls=%d want=%v", err, modelCalls, test.want)
		}
	}
}

func TestSearchbotNumericIdentityAndDefaults(t *testing.T) {
	id, modelName, kind, key, base := int64(42), "same-name", "chat", "private-key", "https://owned.invalid/v1"
	factory := &searchbotTestFactory{driver: &searchbotTestDriver{}}
	catalog := &entity.ProviderManager{Providers: []entity.Provider{{Name: "OpenAI", URL: map[string]string{"default": "https://catalog.invalid/v1"}}}}
	s := NewSearchbotService(NewModelProviderServiceWithCatalog(catalog, factory))
	s.loadTenant = func(context.Context, string) (*entity.Tenant, error) {
		active := "1"
		return &entity.Tenant{ID: "owner", Status: &active, TenantLLMID: &id, LLMID: "wrong@instance@OpenAI"}, nil
	}
	row := &entity.TenantLLM{ID: id, TenantID: "owner", Status: "1", LLMFactory: "OpenAI", LLMName: &modelName, ModelType: &kind, APIKey: &key, APIBase: &base, MaxTokens: 0}
	s.loadLegacy = func(_ context.Context, tenant string, gotID int64) (*entity.TenantLLM, error) {
		if tenant != "owner" || gotID != id {
			t.Fatal("wrong numeric query")
		}
		return row, nil
	}
	bound, err := s.chatModel(context.Background(), "owner", nil)
	if err != nil || bound == nil || *bound.ModelName != modelName || *bound.APIConfig.APIKey != key || bound.ModelConfig.MaxTokens == nil || *bound.ModelConfig.MaxTokens != 0 || factory.calls != 1 || factory.urls["default"] != base {
		t.Fatalf("numeric binding err=%v calls=%d", err, factory.calls)
	}
	if catalog.Providers[0].URL["default"] != "https://catalog.invalid/v1" {
		t.Fatal("catalog mutated")
	}
	for _, mutate := range []func(*entity.TenantLLM){func(r *entity.TenantLLM) { r.TenantID = "foreign" }, func(r *entity.TenantLLM) { r.Status = "0" }, func(r *entity.TenantLLM) { v := "embedding"; r.ModelType = &v }, func(r *entity.TenantLLM) { r.ID = 43 }} {
		bad := *row
		mutate(&bad)
		s.loadLegacy = func(context.Context, string, int64) (*entity.TenantLLM, error) { return &bad, nil }
		if _, err := s.chatModel(context.Background(), "owner", "42"); err == nil || factory.calls != 1 {
			t.Fatalf("bad numeric row bound: err=%v calls=%d", err, factory.calls)
		}
	}
}

func TestSearchbotCancellationAfterDriver(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s := NewSearchbotService(nil)
	s.loadPrompt = func(string) (string, error) { return "prompt", nil }
	name := "model"
	driver := &searchbotTestDriver{chat: func(_ string, api *models.APIConfig, _ []models.Message, _ *models.ChatConfig) (*models.ChatResponse, error) {
		if api.Context != ctx {
			t.Fatal("cancellation context lost")
		}
		cancel()
		answer := "1. late answer"
		return &models.ChatResponse{Answer: &answer}, nil
	}}
	s.bindModel = func(context.Context, string, any) (*models.ChatModel, error) {
		return models.NewChatModel(driver, &name, nil), nil
	}
	if _, err := s.RelatedQuestions(ctx, "owner", "question", ""); !errors.Is(err, context.Canceled) {
		t.Fatalf("late response accepted: %v", err)
	}
}

func TestSearchbotParseQuestions(t *testing.T) {
	for _, test := range []struct {
		input string
		want  []string
	}{
		{"", []string{}}, {"no suggestions", []string{}},
		{"  1. first  \n10. tenth\nnot numbered\n2. ", []string{"first", "tenth"}},
		{"<think>\n1. hidden forever", []string{}},
	} {
		if got := parseSearchbotQuestions(test.input); !reflect.DeepEqual(got, test.want) {
			t.Errorf("parse %q = %v, want %v", test.input, got, test.want)
		}
	}
}

func TestSearchbotEverySelectorRequiresActiveOwner(t *testing.T) {
	for _, selector := range []any{"42", "same-name@default@OpenAI"} {
		for _, tenant := range []*entity.Tenant{nil, {ID: "owner"}, {ID: "foreign"}} {
			factory := &searchbotTestFactory{driver: &searchbotTestDriver{}}
			s := NewSearchbotService(NewModelProviderServiceWithCatalog(&entity.ProviderManager{}, factory))
			s.loadTenant = func(context.Context, string) (*entity.Tenant, error) { return tenant, nil }
			s.loadLegacy = func(context.Context, string, int64) (*entity.TenantLLM, error) {
				t.Fatal("inactive owner reached legacy model query")
				return nil, nil
			}
			if _, err := s.chatModel(context.Background(), "owner", selector); err == nil || factory.calls != 0 {
				t.Fatal("inactive owner constructed a provider")
			}
		}
	}
}
