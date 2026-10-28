package service

import (
	"context"
	"database/sql"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/testsupport"
)

// TestAfter67SearchScopeSQLHTTP exercises actual SQL authorization and the real
// provider HTTP request without a vendor account or shared application data.
func TestAfter67SearchScopeSQLHTTP(t *testing.T) {
	config := os.Getenv("MULTIRAG_AFTER67_S_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned S SQL config required")
		}
		t.Skip("owned S SQL config required")
	}
	journal := testsupport.NewCase(t)
	raw, err := os.ReadFile(config)
	if err != nil {
		t.Fatal("private scratch config unavailable")
	}
	var cfg struct {
		Host, User, Password, Database string
		Port                           int
	}
	if json.Unmarshal(raw, &cfg) != nil || cfg.Host != "127.0.0.1" || cfg.Port != 5432 || cfg.Database != "multirag_batch_s_after67_20261029" {
		t.Fatal("exact owned TCP database required")
	}
	endpoint := url.URL{Scheme: "postgres", User: url.UserPassword(cfg.User, cfg.Password), Host: "127.0.0.1:5432", Path: cfg.Database, RawQuery: "sslmode=disable"}
	journal.Event("reserve-schema-tables-before-SQL", map[string]any{"host": cfg.Host, "port": cfg.Port, "database": cfg.Database, "schema": "public", "tables": []string{"t_ai_search", "t_ai_user_tenants", "t_ai_tenant_llms", "t_ai_tenants", "tenant_model_provider", "tenant_model_instance", "tenant_model", "t_ai_knowledgebases"}})
	db, err := gorm.Open(postgres.Open(endpoint.String()), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("scratch SQL connection failed")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = pool.Close() })
	old := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = old })
	read, err := sql.Open("pgx", endpoint.String())
	if err != nil {
		t.Fatal("independent SQL connection failed")
	}
	t.Cleanup(func() { _ = read.Close() })
	if err := db.AutoMigrate(&entity.Search{}, &entity.UserTenant{}, &entity.TenantLLM{}, &entity.Tenant{}, &entity.TenantModelProvider{}, &entity.TenantModelInstance{}, &entity.TenantModel{}, &entity.Knowledgebase{}); err != nil {
		t.Fatal("scratch schema failed")
	}
	t.Chdir(filepath.Join("..", ".."))
	catalog, err := entity.NewProviderManager(filepath.Join("configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	var calls atomic.Int64
	slow := atomic.Bool{}
	entered := make(chan struct{}, 1)
	fixture := registeredModelServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/chat/completions" || r.Header.Get("Authorization") != "Bearer owned-legacy-key" {
			t.Error("wrong model binding")
			w.WriteHeader(400)
			return
		}
		var body map[string]any
		if json.NewDecoder(r.Body).Decode(&body) != nil || body["model"] != "owned-chat" || body["temperature"] != float64(0) {
			t.Error("zero generation config or model lost")
			w.WriteHeader(400)
			return
		}
		if slow.Load() {
			select {
			case entered <- struct{}{}:
			default:
			}
			<-r.Context().Done()
			return
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"choices":[{"message":{"content":"1. First question\n10. Tenth question"}}]}`)
	}))
	active, inactive, chat, embed, key, name, base := "1", "0", "chat", "embedding", "owned-legacy-key", "owned-chat", fixture.URL
	owner, foreign := "s-owner", "s-foreign"
	journal.Event("reserve-fixture-namespaces-before-INSERT", map[string]any{"tenant_ids": []string{owner, foreign}, "user_ids": []string{owner, "s-member", "s-invite", "s-unknown", "s-inactive"}, "search_ids": []string{"s-search", "s-foreign-search", "s-deleted"}, "dataset_ids": []string{"s-kb"}, "legacy_model_natural_keys": []any{[]string{owner, "OpenAI", name}, []string{foreign, "OpenAI", "foreign"}, []string{owner, "OpenAI", "disabled"}, []string{owner, "OpenAI", "embedding"}}, "modern_ids": []string{"s-provider", "s-instance", "s-model"}})
	create := func(value any) {
		t.Helper()
		if err := db.Create(value).Error; err != nil {
			t.Fatalf("fixture INSERT failed: %T", value)
		}
	}
	create(&entity.Tenant{ID: owner, Status: &active})
	create(&entity.Tenant{ID: foreign, Status: &active})
	legacy := &entity.TenantLLM{TenantID: owner, LLMFactory: "OpenAI", LLMName: &name, ModelType: &chat, APIKey: &key, APIBase: &base, Status: active}
	create(legacy)
	numeric := func(tenant, modelName, kind, status string) int64 {
		row := &entity.TenantLLM{TenantID: tenant, LLMFactory: "OpenAI", LLMName: &modelName, ModelType: &kind, APIKey: &key, APIBase: &base, Status: status}
		create(row)
		return row.ID
	}
	foreignID := numeric(foreign, "foreign", chat, active)
	disabledID := numeric(owner, "disabled", chat, inactive)
	wrongType := numeric(owner, "embedding", embed, active)
	if err := db.Model(&entity.Tenant{}).Where("id = ?", owner).Update("tenant_llm_id", legacy.ID).Error; err != nil {
		t.Fatal("default model update failed")
	}
	// A modern same-name binding must never substitute for an explicit legacy ID.
	create(&entity.TenantModelProvider{ID: "s-provider", ProviderName: "OpenAI", TenantID: owner})
	create(&entity.TenantModelInstance{ID: "s-instance", ProviderID: "s-provider", InstanceName: "default", APIKey: "wrong-modern-key", Status: "active", Extra: `{"base_url":"http://127.0.0.1:1"}`})
	create(&entity.TenantModel{ID: "s-model", ProviderID: "s-provider", InstanceID: "s-instance", ModelName: name, ModelType: chat, Status: "active"})
	for _, m := range []struct{ user, role, status string }{{owner, "owner", active}, {"s-member", "normal", active}, {"s-invite", "invite", active}, {"s-unknown", "unknown", active}, {"s-inactive", "normal", inactive}} {
		create(&entity.UserTenant{ID: m.user, UserID: m.user, TenantID: owner, Role: m.role, Status: &m.status})
	}
	create(&entity.Knowledgebase{ID: "s-kb", TenantID: owner, CreatedBy: owner, Permission: "team", Status: &active})
	saved := &entity.Search{ID: "s-search", TenantID: owner, CreatedBy: owner, Name: "owned", Status: &active, SearchConfig: entity.JSONMap{"chat_id": legacy.ID, "llm_setting": map[string]any{"temperature": 0}}}
	create(saved)
	create(&entity.Search{ID: "s-foreign-search", TenantID: foreign, CreatedBy: foreign, Status: &active, SearchConfig: entity.JSONMap{}})
	create(&entity.Search{ID: "s-deleted", TenantID: owner, CreatedBy: owner, Status: &inactive, SearchConfig: entity.JSONMap{}})
	var storedID int64
	if err := read.QueryRow("SELECT id FROM t_ai_tenant_llms WHERE tenant_id=$1 AND llm_factory=$2 AND llm_name=$3", owner, "OpenAI", name).Scan(&storedID); err != nil || storedID != legacy.ID || storedID <= 0 {
		t.Fatal("numeric identity independent readback failed")
	}
	journal.Event("model-ID-independent-readback", map[string]any{"tenant": owner, "model_id": storedID, "foreign_model_id": foreignID, "disabled_model_id": disabledID, "wrong_type_model_id": wrongType})
	service := NewSearchbotService(NewModelProviderServiceWithCatalog(catalog, nil))
	for _, actor := range []string{owner, "s-member"} {
		got, err := service.RelatedQuestions(context.Background(), actor, "question", "s-search")
		if err != nil || !reflect.DeepEqual(got, []string{"First question", "Tenth question"}) {
			t.Fatalf("authorized suggestions failed: %v", err)
		}
	}
	before := calls.Load()
	for _, tc := range []struct{ actor, id string }{{"s-member", "s-foreign-search"}, {owner, "missing"}, {owner, "s-deleted"}, {"s-invite", "s-search"}, {"s-unknown", "s-search"}, {"s-inactive", "s-search"}} {
		if _, err := service.RelatedQuestions(context.Background(), tc.actor, "question", tc.id); !errors.Is(err, ErrSearchbotAccessDenied) {
			t.Fatalf("inaccessible search accepted: %s %s", tc.actor, tc.id)
		}
	}
	if calls.Load() != before {
		t.Fatal("unauthorized search invoked provider")
	}
	if err := db.Model(&entity.UserTenant{}).Where("id = ?", owner).Update("status", "0").Error; err != nil {
		t.Fatal(err)
	}
	if _, err := service.RelatedQuestions(context.Background(), owner, "q", saved.ID); !errors.Is(err, ErrSearchbotAccessDenied) || calls.Load() != before {
		t.Fatal("revoked owner membership reached model")
	}
	if err := db.Model(&entity.UserTenant{}).Where("id = ?", owner).Update("status", "1").Error; err != nil {
		t.Fatal(err)
	}

	for _, bad := range []int64{foreignID, disabledID, wrongType, 999999} {
		if err := db.Model(&entity.Search{}).Where("id = ?", saved.ID).Update("search_config", entity.JSONMap{"chat_id": bad, "llm_setting": map[string]any{"temperature": 0}}).Error; err != nil {
			t.Fatal(err)
		}
		if _, err := service.RelatedQuestions(context.Background(), owner, "question", saved.ID); err == nil {
			t.Fatal("unavailable numeric model accepted")
		}
	}
	if calls.Load() != before {
		t.Fatal("invalid model invoked provider")
	}
	// The stored numeric default remains authoritative when chat_id is omitted.
	if err := db.Model(&entity.Search{}).Where("id = ?", saved.ID).Update("search_config", entity.JSONMap{"llm_setting": map[string]any{"temperature": 0}}).Error; err != nil {
		t.Fatal(err)
	}
	if _, err := service.RelatedQuestions(context.Background(), owner, "question", saved.ID); err != nil {
		t.Fatal("numeric tenant default failed")
	}
	before = calls.Load()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := service.RelatedQuestions(ctx, owner, "question", saved.ID); !errors.Is(err, context.Canceled) || calls.Load() != before {
		t.Fatal("pre-canceled request performed provider work")
	}
	slow.Store(true)
	ctx, cancel = context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { _, err := service.RelatedQuestions(ctx, owner, "question", saved.ID); done <- err }()
	select {
	case <-entered:
		cancel()
	case <-time.After(3 * time.Second):
		cancel()
		t.Fatal("slow provider not entered")
	}
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("cancellation lost: %T", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("canceled provider call failed to drain")
	}

	metadataStore := &after67MetadataEngine{}
	chunks := &ChunkService{kbDAO: dao.NewKnowledgebaseDAO(), docEngine: metadataStore}
	request := &RetrievalTestRequest{DatasetIDs: []string{"s-kb"}, Question: "q", DocIDs: []string{}, Filter: map[string]any{"method": "auto"}}
	beforeEmpty := calls.Load()
	if got, err := chunks.RetrievalTest(context.Background(), request, "s-member"); err != nil || got == nil || got.Total != 0 || metadataStore.calls != 0 || calls.Load() != beforeEmpty {
		t.Fatal("explicit empty scope reached metadata or model")
	}
	foreignSearch := "s-foreign-search"
	request.SearchID = &foreignSearch
	if _, err := chunks.RetrievalTest(context.Background(), request, "s-member"); !errors.Is(err, ErrSearchbotAccessDenied) {
		t.Fatal("foreign search must be denied even for empty scope")
	}
	request.SearchID = nil
	request.DocIDs = nil
	request.Filter = manualMetadataConfig([]MetaFilterCondition{{Key: "field", Op: "not in", Value: false}}, "or")
	if _, err := chunks.RetrievalTest(context.Background(), request, "s-member"); err == nil || metadataStore.calls != 0 {
		t.Fatal("invalid metadata predicate reached index")
	}
	request.Filter = map[string]any{"method": "semi_auto", "semi_auto": []any{"missing"}}
	if got, err := chunks.RetrievalTest(context.Background(), request, "s-member"); err != nil || got.Total != 0 || metadataStore.calls != 1 || calls.Load() != beforeEmpty {
		t.Fatal("unknown selected metadata key reached model")
	}
	metadataStore.err = errors.New("controlled metadata failure")
	request.Filter = manualMetadataConfig([]MetaFilterCondition{{Key: "field", Op: "is", Value: "value"}}, "and")
	if _, err := chunks.RetrievalTest(context.Background(), request, "s-member"); err == nil || calls.Load() != beforeEmpty {
		t.Fatal("metadata IO failure became unfiltered retrieval")
	}
	metadataStore.err = nil
	if got, err := chunks.RetrievalTest(context.Background(), request, "s-member"); err != nil || got.Total != 0 || calls.Load() != beforeEmpty {
		t.Fatal("zero metadata matches became unfiltered retrieval")
	}

	request.SearchID = &saved.ID
	beforeInvalid := metadataStore.calls
	for _, badConfig := range []entity.JSONMap{{"meta_data_filter": []any{}}, {"meta_data_filter": nil}, {"meta_data_filter": "bad"}, {"chat_id": nil}} {
		if err := db.Model(&entity.Search{}).Where("id = ?", saved.ID).Update("search_config", badConfig).Error; err != nil {
			t.Fatal(err)
		}
		if _, err := chunks.RetrievalTest(context.Background(), request, "s-member"); err == nil || metadataStore.calls != beforeInvalid {
			t.Fatal("invalid saved filter reached metadata work")
		}
	}
	if err := db.Model(&entity.Search{}).Where("id = ?", saved.ID).Update("search_config", entity.JSONMap{"llm_setting": map[string]any{"temperature": 0}}).Error; err != nil {
		t.Fatal(err)
	}
	request.SearchID = nil
	ctx, cancel = context.WithCancel(context.Background())
	metadataStore.expectedContext = ctx
	metadataStore.cancel = cancel
	if _, err := chunks.RetrievalTest(ctx, request, "s-member"); !errors.Is(err, context.Canceled) {
		t.Fatal("metadata cancellation became empty success")
	}
	metadataStore.expectedContext = nil
	metadataStore.cancel = nil
	journal.Event("retrieval-empty-filter-independent-readback", map[string]any{"explicit_empty_metadata_calls": 0, "invalid_predicate_metadata_calls": 0, "foreign_search_model_calls": 0, "unknown_selected_key_model_calls": 0, "failed_metadata_fallback": false, "no_match_retrieval": false})

	metadataStore.chunks = []map[string]any{{"id": "zero-doc", "meta_fields": map[string]any{"boolean": false, "number": float64(0), "empty": "", "labels": []any{false, ""}, "empty_metadata": true}}}
	directory, err := (&MetadataService{kbDAO: dao.NewKnowledgebaseDAO(), docEngine: metadataStore}).GetFlattedMetaByKBsContext(context.Background(), []string{"s-kb"})
	if err != nil || directory["empty_metadata"] != nil {
		t.Fatal("metadata flattening exposed a control field")
	}
	for _, condition := range []MetaFilterCondition{{Key: "boolean", Op: "is", Value: false}, {Key: "number", Op: "is", Value: 0}, {Key: "empty", Op: "empty"}, {Key: "labels", Op: "is", Value: false}} {
		ids, empty, err := ApplyMetaDataFilterChecked(context.Background(), manualMetadataConfig([]MetaFilterCondition{condition}, "and"), directory, "q", nil, nil)
		if err != nil || empty || !reflect.DeepEqual(ids, []string{"zero-doc"}) {
			t.Fatalf("actual metadata scalar lost: %s %v", condition.Key, ids)
		}
	}

	packed := []byte{}
	for i := 0; i < 2; i++ {
		raw := []byte(`{"boolean":false,"number":0,"empty":"","labels":[false,""]}`)
		prefix := make([]byte, 4)
		binary.LittleEndian.PutUint32(prefix, uint32(len(raw)))
		packed = append(packed, prefix...)
		packed = append(packed, raw...)
	}
	metadataStore.chunks = []map[string]any{{"id": "packed-doc", "meta_fields": packed}}
	directory, err = (&MetadataService{kbDAO: dao.NewKnowledgebaseDAO(), docEngine: metadataStore}).GetFlattedMetaByKBsContext(context.Background(), []string{"s-kb"})
	if err != nil {
		t.Fatal(err)
	}
	for _, condition := range []MetaFilterCondition{{Key: "boolean", Op: "is", Value: false}, {Key: "number", Op: "is", Value: 0}, {Key: "empty", Op: "empty"}, {Key: "labels", Op: "is", Value: false}} {
		ids, empty, err := ApplyMetaDataFilterChecked(context.Background(), manualMetadataConfig([]MetaFilterCondition{condition}, "and"), directory, "q", nil, nil)
		if err != nil || empty || !reflect.DeepEqual(ids, []string{"packed-doc"}) {
			t.Fatal("packed duplicate metadata fields lost a JSON scalar")
		}
	}
	journal.Event("actual-flat-metadata-readback", map[string]any{"false_preserved": true, "zero_preserved": true, "empty_string_preserved": true, "empty_metadata_control_removed": true, "model_calls": 0})

	var configJSON []byte
	if err := read.QueryRow("SELECT search_config FROM t_ai_search WHERE id=$1", saved.ID).Scan(&configJSON); err != nil {
		t.Fatal("search config independent readback failed")
	}
	if !strings.Contains(string(configJSON), `"temperature": 0`) && !strings.Contains(string(configJSON), `"temperature":0`) {
		t.Fatal("zero stored setting changed")
	}
	// All queries above are reads; no new models/searches/memberships appear.
	for table, want := range map[string]int{"t_ai_search": 3, "t_ai_tenant_llms": 4, "t_ai_user_tenants": 5, "t_ai_tenants": 2} {
		var count int
		if err := read.QueryRow("SELECT count(*) FROM " + table).Scan(&count); err != nil || count != want {
			t.Fatal("forbidden SQL side effect")
		}
	}
	journal.Event("search-model-contract-independent-readback", map[string]any{"tenant": owner, "model_id": strconv.FormatInt(legacy.ID, 10), "provider_calls": calls.Load(), "denied_provider_calls": 0, "unexpected_SQL_writes": 0, "numeric_default": true, "zero_temperature": true, "modern_collision_retained_legacy": true, "cancellation_drained": true})
}

type after67MetadataEngine struct {
	engine.DocEngine
	calls           int
	err             error
	expectedContext context.Context
	cancel          context.CancelFunc
	chunks          []map[string]any
}

func (e *after67MetadataEngine) Search(ctx context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	e.calls++
	if e.expectedContext != nil && ctx != e.expectedContext {
		return nil, errors.New("metadata request context lost")
	}
	if e.cancel != nil {
		e.cancel()
	}
	if len(req.IndexNames) != 1 || req.IndexNames[0] != "multirag_doc_meta_s-owner" || !reflect.DeepEqual(req.KbIDs, []string{"s-kb"}) {
		return nil, errors.New("metadata escaped authorized dataset scope")
	}
	return &types.SearchResult{Chunks: e.chunks}, e.err
}
