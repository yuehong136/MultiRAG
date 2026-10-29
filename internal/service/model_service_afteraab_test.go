package service

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"reflect"
	"sync/atomic"
	"testing"
	"time"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/testsupport"
)

// This case uses only the explicitly owned TCP scratch database. The independent
// SQL connection verifies fixtures, failed-call side effects and exact cleanup.
func TestAfteraabModelBindingsSQLHTTP(t *testing.T) {
	configPath := os.Getenv("MULTIRAG_AFTERAAB_M_CONFIG")
	if configPath == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned M SQL config required")
		}
		t.Skip("owned M SQL config required")
	}
	journal := testsupport.NewCase(t)
	raw, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal("owned M SQL config unavailable")
	}
	var cfg struct {
		Host, User, Password, Database string
		Port                           int
	}
	if json.Unmarshal(raw, &cfg) != nil || cfg.Host != "127.0.0.1" || cfg.Port != 5432 || cfg.Database != "multirag_go_models_afteraab_m_20261029" {
		t.Fatal("exact owned M TCP database required")
	}
	endpoint := url.URL{Scheme: "postgres", User: url.UserPassword(cfg.User, cfg.Password), Host: "127.0.0.1:5432", Path: cfg.Database, RawQuery: "sslmode=disable"}
	tables := []string{"t_ai_tenants", "t_ai_tenant_llms", "tenant_model_provider", "tenant_model_instance", "tenant_model"}
	journal.Event("reserve-schema-before-AutoMigrate", map[string]any{"host": cfg.Host, "port": cfg.Port, "database": cfg.Database, "schema": "public", "tables": tables})
	db, err := gorm.Open(postgres.Open(endpoint.String()), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("owned M SQL connection failed")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	read, err := sql.Open("pgx", endpoint.String())
	if err != nil {
		t.Fatal("independent M SQL connection failed")
	}
	t.Cleanup(func() { read.Close() })
	oldDB := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = oldDB })
	if err := db.AutoMigrate(&entity.Tenant{}, &entity.TenantLLM{}, &entity.TenantModelProvider{}, &entity.TenantModelInstance{}, &entity.TenantModel{}); err != nil {
		t.Fatal("owned M schema migration failed")
	}
	for _, table := range tables {
		var count int
		if err := read.QueryRow("SELECT count(*) FROM " + table).Scan(&count); err != nil || count != 0 {
			t.Fatal("owned M test requires fresh empty tables", table)
		}
	}
	const owner, other, missing = "m-owner", "m-other", "m-missing"
	const builtinName, modernName = "owned-tei", "owned-embedding"
	providerName := "OpenAI-API-Compatible"
	legacyProviderName := "OpenAI"
	var calls atomic.Int64
	var slow atomic.Bool
	entered := make(chan struct{}, 1)
	stopped := make(chan struct{}, 1)
	server := afteraabModelBindingServer(t, journal, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/embed" || r.Header.Get("Authorization") != "" || r.Header.Get("api-key") != "" {
			t.Error("Builtin endpoint or credential isolation failed")
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		var body struct {
			Inputs []string `json:"inputs"`
		}
		if json.NewDecoder(r.Body).Decode(&body) != nil || !reflect.DeepEqual(body.Inputs, []string{"owned input"}) {
			t.Error("Builtin input binding changed")
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if slow.Load() {
			w.WriteHeader(http.StatusOK)
			w.(http.Flusher).Flush()
			entered <- struct{}{}
			<-r.Context().Done()
			stopped <- struct{}{}
			return
		}
		fmt.Fprint(w, "[[0,1]]")
	}))
	t.Setenv("COMPOSE_PROFILES", "postgres,tei-cpu")
	t.Setenv("TEI_MODEL", builtinName)
	t.Setenv("TEI_BASE_URL", server.URL)
	active, inactive, embedding, chat := "1", "0", "embedding", "chat"
	builtinComposite := builtinName + "@Builtin"
	modernComposite := modernName + "@default@" + providerName
	journal.Event("reserve-fixtures-before-INSERT", map[string]any{"tenant_ids": []string{owner, other}, "legacy_bindings": []any{[]string{owner, "Builtin", builtinName}, []string{other, "Builtin", builtinName}, []string{owner, legacyProviderName, "legacy-embedding"}, []string{other, legacyProviderName, "legacy-embedding"}}, "provider_ids": []string{"m-provider-owner", "m-provider-other"}, "instance_ids": []string{"m-instance-owner", "m-instance-other"}, "model_ids": []string{"m-model-owner", "m-model-other", "m-model-other-name", "m-model-foreign-instance"}, "TEI_endpoint": server.URL, "TEI_model": builtinName, "TEI_profile": "tei-cpu", "inputs": []string{"owned input"}})
	create := func(value any) {
		t.Helper()
		if err := db.Create(value).Error; err != nil {
			t.Fatalf("owned M fixture INSERT failed: %T", value)
		}
	}
	create(&entity.Tenant{ID: owner, Status: &active, EmbdID: builtinComposite})
	create(&entity.Tenant{ID: other, Status: &active, EmbdID: builtinComposite})
	builtin := &entity.TenantLLM{TenantID: owner, LLMFactory: "Builtin", LLMName: afteraabModelString(builtinName), ModelType: &embedding, Status: active}
	create(builtin)
	create(&entity.TenantLLM{TenantID: other, LLMFactory: "Builtin", LLMName: afteraabModelString(builtinName), ModelType: &embedding, Status: active})
	for _, tenant := range []string{owner, other} {
		key := "private-legacy-" + tenant
		create(&entity.TenantLLM{TenantID: tenant, LLMFactory: legacyProviderName, LLMName: afteraabModelString("legacy-embedding"), ModelType: &embedding, APIKey: &key, APIBase: &server.URL, Status: active})
	}
	for _, item := range []struct{ tenant, suffix string }{{owner, "owner"}, {other, "other"}} {
		providerID, instanceID := "m-provider-"+item.suffix, "m-instance-"+item.suffix
		create(&entity.TenantModelProvider{ID: providerID, TenantID: item.tenant, ProviderName: providerName})
		extra, err := encodeProviderInstanceExtra("default", server.URL)
		if err != nil {
			t.Fatal(err)
		}
		create(&entity.TenantModelInstance{ID: instanceID, ProviderID: providerID, InstanceName: "default", APIKey: "private-key-" + item.suffix, Status: "active", Extra: extra})
		create(&entity.TenantModel{ID: "m-model-" + item.suffix, ProviderID: providerID, InstanceID: instanceID, ModelName: modernName, ModelType: embedding, Status: "active", Extra: `{"max_tokens":0}`})
	}
	create(&entity.TenantModel{ID: "m-model-other-name", ProviderID: "m-provider-other", InstanceID: "m-instance-other", ModelName: "different-model", ModelType: embedding, Status: "active", Extra: `{"max_tokens":0}`})
	// This row points at another tenant's instance. The DAO must retain both
	// provider and instance conditions instead of selecting solely by name.
	create(&entity.TenantModel{ID: "m-model-foreign-instance", ProviderID: "m-provider-owner", InstanceID: "m-instance-other", ModelName: "foreign-model", ModelType: embedding, Status: "active", Extra: `{"max_tokens":0}`})
	t.Cleanup(func() {
		journal.Event("cleanup-owned-fixture-rows", map[string]any{"database": cfg.Database, "tables": tables})
		for _, query := range []string{
			"DELETE FROM tenant_model WHERE id IN ('m-model-owner','m-model-other','m-model-other-name','m-model-foreign-instance')",
			"DELETE FROM tenant_model_instance WHERE id IN ('m-instance-owner','m-instance-other')",
			"DELETE FROM tenant_model_provider WHERE id IN ('m-provider-owner','m-provider-other')",
			"DELETE FROM t_ai_tenant_llms WHERE tenant_id IN ('m-owner','m-other')",
			"DELETE FROM t_ai_tenants WHERE id IN ('m-owner','m-other')",
		} {
			if _, err := read.Exec(query); err != nil {
				t.Error("owned M fixture cleanup failed")
			}
		}
		counts, readbackOK := map[string]int{}, true
		for _, table := range tables {
			var count int
			if err := read.QueryRow("SELECT count(*) FROM " + table).Scan(&count); err != nil {
				readbackOK = false
				t.Error("owned M fixture cleanup readback failed", table)
				continue
			}
			counts[table] = count
			if count != 0 {
				readbackOK = false
				t.Error("owned M fixture rows survived cleanup", table)
			}
		}
		journal.Event("cleanup-owned-fixture-readback", map[string]any{"database": cfg.Database, "table_counts": counts, "readback_ok": readbackOK})
	})
	var storedName, storedType, storedStatus string
	if err := read.QueryRow("SELECT llm_name,mdl_type,status FROM t_ai_tenant_llms WHERE id=$1 AND tenant_id=$2", builtin.ID, owner).Scan(&storedName, &storedType, &storedStatus); err != nil || storedName != builtinName || storedType != embedding || storedStatus != active {
		t.Fatal("independent SQL Builtin binding readback failed")
	}
	journal.Event("independent-binding-readback", map[string]any{"tenant": owner, "numeric_binding_id": builtin.ID, "name": storedName, "capability": storedType, "status": storedStatus})
	catalog := &entity.ProviderManager{Providers: []entity.Provider{
		{Name: providerName, URL: map[string]string{"default": server.URL}, URLSuffix: models.URLSuffix{Embedding: "embeddings"}},
		{Name: legacyProviderName, URL: map[string]string{"default": server.URL}, URLSuffix: models.URLSuffix{Embedding: "embeddings"}},
	}}
	service := NewModelProviderServiceWithCatalog(catalog, nil)
	modernBound, err := service.GetEmbeddingModelWithContext(t.Context(), owner, modernComposite)
	if err != nil || modernBound.APIConfig.Context != t.Context() || modernBound.ModelName == nil || *modernBound.ModelName != modernName || modernBound.APIConfig.APIKey == nil || *modernBound.APIConfig.APIKey != "private-key-owner" {
		t.Fatal("modern SQL binding lost request context or tenant credentials")
	}
	storedModern, err := dao.NewTenantModelDAO().GetModelByProviderIDAndInstanceIDAndModelNameWithContext(t.Context(), "m-provider-owner", "m-instance-owner", modernName)
	if err != nil || storedModern.ID != "m-model-owner" {
		t.Fatal("context DAO did not preserve provider and instance scope")
	}
	for _, selected := range []string{builtinComposite, ""} {
		bound, err := service.GetEmbeddingModelWithContext(t.Context(), owner, selected)
		if err != nil {
			t.Fatal("authorized Builtin model did not bind", err)
		}
		bound.EmbeddingConfig = &models.EmbeddingConfig{Dimension: 0}
		data, err := bound.Embed([]string{"owned input"})
		if err != nil || len(data) != 1 || data[0].Index != 0 || !reflect.DeepEqual(data[0].Embedding, []float64{0, 1}) || bound.APIConfig.Context != t.Context() {
			t.Fatal("bound Builtin embedding/default/context failed", data, err)
		}
	}
	snapshot := func() []string {
		t.Helper()
		var result []string
		for _, table := range tables {
			var digest string
			query := "SELECT md5(COALESCE(string_agg(row_to_json(r)::text, '|' ORDER BY row_to_json(r)::text), '')) FROM " + table + " r"
			if err := read.QueryRow(query).Scan(&digest); err != nil {
				t.Fatal("independent table digest readback failed")
			}
			result = append(result, digest)
		}
		var used int64
		if err := read.QueryRow("SELECT COALESCE(sum(used_tokens),0) FROM t_ai_tenant_llms").Scan(&used); err != nil {
			t.Fatal("independent usage readback failed")
		}
		return append(result, fmt.Sprint(used))
	}
	deny := func(name string, action func() error) {
		t.Helper()
		beforeCalls, beforeRows := calls.Load(), snapshot()
		if err := action(); err == nil {
			t.Fatal("invalid model binding accepted", name)
		}
		if calls.Load() != beforeCalls || !reflect.DeepEqual(beforeRows, snapshot()) {
			t.Fatal("denied binding caused HTTP or SQL side effects", name)
		}
		journal.Event("binding-denied-zero-side-effect-readback", map[string]any{"scenario": name, "HTTP_delta": 0, "table_row_digests_and_usage": beforeRows})
	}
	bind := func(tenant, name string) func() error {
		return func() error { _, err := service.GetEmbeddingModelWithContext(t.Context(), tenant, name); return err }
	}
	update := func(model any, id any, column string, value any) {
		t.Helper()
		if err := db.Model(model).Where("id = ?", id).Update(column, value).Error; err != nil {
			t.Fatal("owned M fixture mutation failed", column)
		}
	}
	deny("missing tenant", bind(missing, builtinComposite))
	deny("wrong Builtin instance", bind(owner, builtinName+"@foreign@Builtin"))
	deny("wrong configured model", bind(owner, "other-model@Builtin"))
	deny("wrong Builtin capability", func() error {
		_, err := service.GetRerankModelWithContext(t.Context(), owner, builtinComposite)
		return err
	})
	update(&entity.Tenant{}, owner, "status", inactive)
	deny("inactive Builtin tenant", bind(owner, builtinComposite))
	deny("inactive modern tenant", func() error {
		_, err := service.ResolvedEmbeddingIdentity(t.Context(), owner, modernComposite)
		return err
	})
	update(&entity.Tenant{}, owner, "status", active)
	update(&entity.TenantLLM{}, builtin.ID, "status", inactive)
	deny("disabled Builtin binding", bind(owner, builtinComposite))
	update(&entity.TenantLLM{}, builtin.ID, "status", active)
	update(&entity.TenantLLM{}, builtin.ID, "mdl_type", chat)
	deny("wrong Builtin binding capability", bind(owner, builtinComposite))
	update(&entity.TenantLLM{}, builtin.ID, "mdl_type", embedding)
	t.Setenv("COMPOSE_PROFILES", "not-tei-cpu")
	deny("profile substring cannot enable TEI", bind(owner, builtinComposite))
	t.Setenv("COMPOSE_PROFILES", "postgres,tei-cpu")
	t.Setenv("TEI_BASE_URL", "")
	deny("missing explicit TEI endpoint", bind(owner, builtinComposite))
	t.Setenv("TEI_BASE_URL", server.URL)
	deny("foreign instance row", bind(owner, "foreign-model@default@"+providerName))
	deny("other tenant has no named instance", bind(owner, modernName+"@other@"+providerName))

	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	deny("canceled before bind", func() error {
		_, err := service.GetEmbeddingModelWithContext(ctx, owner, builtinComposite)
		if !errors.Is(err, context.Canceled) {
			return nil
		}
		return err
	})
	deny("canceled context DAO", func() error {
		_, err := dao.NewTenantModelDAO().GetModelByProviderIDAndInstanceIDAndModelNameWithContext(ctx, "m-provider-owner", "m-instance-owner", modernName)
		if !errors.Is(err, context.Canceled) {
			return nil
		}
		return err
	})
	ctx, cancel = context.WithCancel(t.Context())
	bound, err := service.GetEmbeddingModelWithContext(ctx, owner, builtinComposite)
	if err != nil || bound.APIConfig.Context != ctx {
		t.Fatal("request context did not survive SQL binding")
	}
	slow.Store(true)
	done := make(chan error, 1)
	go func() { _, err := bound.Embed([]string{"owned input"}); done <- err }()
	select {
	case <-entered:
		cancel()
	case <-time.After(3 * time.Second):
		cancel()
		t.Fatal("bound provider request did not start")
	}
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatal("bound provider cancellation cause lost", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("bound provider cancellation failed to drain")
	}
	select {
	case <-stopped:
	case <-time.After(3 * time.Second):
		t.Fatal("bound provider response/socket did not close")
	}
	slow.Store(false)

	beforeIdentityHTTP := calls.Load()
	identity := func(tenant, name string) EmbeddingModelIdentity {
		t.Helper()
		result, err := service.ResolvedEmbeddingIdentity(t.Context(), tenant, name)
		if err != nil {
			t.Fatal("valid identity resolution failed", err)
		}
		return result
	}
	ownerIdentity, otherIdentity := identity(owner, modernComposite), identity(other, modernComposite)
	if ownerIdentity != otherIdentity || ownerIdentity.Model != modernName || ownerIdentity.Endpoint != server.URL || ownerIdentity.Region != "default" {
		t.Fatal("same embedding space differed by tenant credentials", ownerIdentity, otherIdentity)
	}
	if identity(owner, builtinComposite) != identity(other, builtinComposite) {
		t.Fatal("shared Builtin embedding identity differed by tenant")
	}
	for _, tt := range []struct{ region, endpoint string }{{"default", server.URL + "/other"}, {"other-region", server.URL}} {
		extra, err := encodeProviderInstanceExtra(tt.region, tt.endpoint)
		if err != nil {
			t.Fatal(err)
		}
		update(&entity.TenantModelInstance{}, "m-instance-other", "extra", extra)
		if identity(other, modernComposite) == ownerIdentity {
			t.Fatal("different endpoint/region shared embedding identity")
		}
	}
	extra, _ := encodeProviderInstanceExtra("default", server.URL)
	update(&entity.TenantModelInstance{}, "m-instance-other", "extra", extra)
	if identity(other, "different-model@default@"+providerName) == ownerIdentity {
		t.Fatal("different model shared embedding identity")
	}
	legacyName := "legacy-embedding@" + legacyProviderName
	if identity(owner, legacyName) != identity(other, legacyName) {
		t.Fatal("same legacy embedding identity differed by credentials")
	}
	update(&entity.TenantModel{}, "m-model-owner", "status", "disabled")
	deny("disabled identity model", func() error {
		_, err := service.ResolvedEmbeddingIdentity(t.Context(), owner, modernComposite)
		return err
	})
	update(&entity.TenantModel{}, "m-model-owner", "status", "active")
	update(&entity.TenantModel{}, "m-model-owner", "model_type", chat)
	deny("wrong identity capability", func() error {
		_, err := service.ResolvedEmbeddingIdentity(t.Context(), owner, modernComposite)
		return err
	})
	update(&entity.TenantModel{}, "m-model-owner", "model_type", embedding)
	deny("missing identity", func() error {
		_, err := service.ResolvedEmbeddingIdentity(t.Context(), owner, "missing@default@"+providerName)
		return err
	})
	update(&entity.TenantModelInstance{}, "m-instance-owner", "status", "disabled")
	deny("disabled identity instance", func() error {
		_, err := service.ResolvedEmbeddingIdentity(t.Context(), owner, modernComposite)
		return err
	})
	update(&entity.TenantModelInstance{}, "m-instance-owner", "status", "active")
	if calls.Load() != beforeIdentityHTTP {
		t.Fatal("read-only identity resolution contacted provider")
	}
	journal.Event("identity-zero-HTTP-readback", map[string]any{"HTTP_delta": 0, "same_space_across_tenants": true, "different_model_endpoint_region_rejected": true})
	journal.Event("reserve-closed-own-pool-failure-case", map[string]any{"database": cfg.Database, "independent_read_pool_retained": true})
	if err := pool.Close(); err != nil {
		t.Fatal("owned SQL pool close failed")
	}
	deny("SQL failure cannot fallback Builtin", bind(owner, builtinComposite))
	deny("SQL failure cannot fallback identity", func() error {
		_, err := service.ResolvedEmbeddingIdentity(t.Context(), owner, modernComposite)
		return err
	})
}

func afteraabModelString(value string) *string { return &value }

func afteraabModelBindingServer(t *testing.T, journal *testsupport.CaseJournal, handler http.Handler) *httptest.Server {
	t.Helper()
	var listener net.Listener
	var address string
	for attempt := 0; attempt < 30; attempt++ {
		var token [2]byte
		if _, err := rand.Read(token[:]); err != nil {
			t.Fatal(err)
		}
		address = fmt.Sprintf("127.0.0.1:%d", 30000+int(binary.BigEndian.Uint16(token[:]))%20000)
		journal.Event("reserve-model-binding-listener-before-bind", map[string]any{"address": address})
		if connection, err := net.DialTimeout("tcp", address, 100*time.Millisecond); err == nil {
			connection.Close()
			journal.Event("model-binding-listener-borrowed-occupied", map[string]any{"address": address})
			continue
		}
		var err error
		listener, err = net.Listen("tcp", address)
		if err == nil {
			break
		}
		journal.Event("model-binding-listener-bind-failed", map[string]any{"address": address})
	}
	if listener == nil {
		t.Fatal("owned M listener allocation failed")
	}
	server := &httptest.Server{Listener: listener, Config: &http.Server{Handler: handler}}
	journal.Event("model-binding-listener-allocated", map[string]any{"address": address, "endpoint": "http://" + address})
	t.Cleanup(func() {
		server.Close()
		connection, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
		absent := err != nil
		if connection != nil {
			connection.Close()
		}
		journal.Event("model-binding-listener-cleanup-readback", map[string]any{"address": address, "absent": absent})
		if !absent {
			t.Error("owned M listener survived cleanup")
		}
	})
	server.Start()
	return server
}
