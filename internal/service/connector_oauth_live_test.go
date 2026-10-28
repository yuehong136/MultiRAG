package service_test

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/service"
	"multirag/internal/testsupport"
)

// The wrapper records the exact randomly generated key before Redis first sees
// it, and retains a complete cleanup list even if the caller is cancelled.
type connectorOAuthRegisteredStore struct {
	store     service.ConnectorOAuthStore
	journal   *testsupport.CaseJournal
	mu        sync.Mutex
	keys      map[string]bool
	afterPeek func()
}

func (s *connectorOAuthRegisteredStore) SetNX(ctx context.Context, key, value string, ttl time.Duration) (bool, error) {
	s.mu.Lock()
	s.journal.Event("reserve-redis-key-before-write", map[string]any{"endpoint": "127.0.0.1:6379", "database": 1, "key": key, "ttl_seconds": ttl.Seconds()})
	s.keys[key] = true
	s.mu.Unlock()
	return s.store.SetNX(ctx, key, value, ttl)
}

func (s *connectorOAuthRegisteredStore) Take(ctx context.Context, key string) (string, error) {
	s.journal.Event("reserve-redis-consume", map[string]any{"endpoint": "127.0.0.1:6379", "database": 1, "key": key})
	return s.store.Take(ctx, key)
}

func (s *connectorOAuthRegisteredStore) PeekOwned(ctx context.Context, key, userID, source, connectorID string) (string, error) {
	s.journal.Event("reserve-owner-redis-peek", map[string]any{"endpoint": "127.0.0.1:6379", "database": 1, "key": key, "user": userID, "source": source, "connector": connectorID})
	raw, err := s.store.PeekOwned(ctx, key, userID, source, connectorID)
	s.mu.Lock()
	hook := s.afterPeek
	s.mu.Unlock()
	if err == nil && hook != nil {
		hook()
	}
	return raw, err
}

func (s *connectorOAuthRegisteredStore) ConsumeOwned(ctx context.Context, key, rawExpected, userID, source, connectorID string) (bool, error) {
	s.journal.Event("reserve-owner-redis-cas-consume", map[string]any{"endpoint": "127.0.0.1:6379", "database": 1, "key": key, "user": userID, "source": source, "connector": connectorID})
	return s.store.ConsumeOwned(ctx, key, rawExpected, userID, source, connectorID)
}

type connectorOAuthTLSMapper struct {
	transport http.RoundTripper
	endpoint  *url.URL
}

func (m connectorOAuthTLSMapper) RoundTrip(req *http.Request) (*http.Response, error) {
	if req.URL.String() != "https://oauth2.googleapis.com/token" {
		return nil, errors.New("unexpected OAuth endpoint")
	}
	copy := req.Clone(req.Context())
	target := *req.URL
	copy.Host = req.URL.Host
	target.Host, target.Scheme = m.endpoint.Host, m.endpoint.Scheme
	copy.URL = &target
	return m.transport.RoundTrip(copy)
}

// Real PostgreSQL authorizes the connector, real Redis arbitrates concurrent
// consumption, and a registered TLS server verifies the actual PKCE exchange.
// Platform JWT route acceptance is covered by the G router acceptance test.
func TestConnectorOAuthLiveSQLRedisAndTLS(t *testing.T) {
	configPath := os.Getenv("MULTIRAG_AFTERFF_G_CONFIG")
	if configPath == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("registered afterff G SQL/Redis configuration required")
		}
		t.Skip("requires registered afterff G SQL/Redis configuration")
	}
	raw, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal("private OAuth configuration unavailable")
	}
	var cfg struct {
		Database server.DatabaseConfig
		Redis    server.RedisConfig
	}
	if json.Unmarshal(raw, &cfg) != nil {
		t.Fatal("invalid private OAuth configuration")
	}
	if host, port, err := net.SplitHostPort(cfg.Redis.Host); err == nil {
		parsedPort, err := strconv.Atoi(port)
		if err != nil {
			t.Fatal("invalid private Redis endpoint")
		}
		cfg.Redis.Host, cfg.Redis.Port = host, parsedPort
	}
	if cfg.Database.Host != "127.0.0.1" || cfg.Database.Port != 5432 || cfg.Database.Database != "multirag_batch_g_afterff_20261028" || cfg.Database.Schema != "usr_ai" || cfg.Redis.Host != "127.0.0.1" || cfg.Redis.Port != 6379 || cfg.Redis.DB != 1 {
		t.Fatal("explicit owned OAuth SQL/Redis endpoints required")
	}
	j := testsupport.NewCase(t)
	ctx := context.Background()
	j.Event("reserve-sql-fixtures", map[string]any{"endpoint": "127.0.0.1:5432", "database": cfg.Database.Database, "schema": cfg.Database.Schema, "tables": []string{"t_ai_users", "t_ai_user_tenants", "t_ai_connectors"}, "users": []string{"oauth19-owner", "oauth19-member", "oauth19-foreign"}, "memberships": []string{"oauth19-member"}, "connectors": []string{"oauth19-drive", "oauth19-gmail", "oauth19-foreign"}})
	dsn, err := dao.PostgresDSN(cfg.Database)
	if err != nil {
		t.Fatal("invalid scratch SQL configuration")
	}
	setup, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("scratch SQL unavailable")
	}
	pool, err := setup.DB()
	if err != nil {
		t.Fatal("scratch SQL pool unavailable")
	}
	t.Cleanup(func() { pool.Close() })
	if err := setup.AutoMigrate(&entity.User{}, &entity.UserTenant{}, &entity.Connector{}); err != nil {
		t.Fatal("scratch OAuth schema migration failed")
	}
	reader, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("independent SQL reader unavailable")
	}
	t.Cleanup(func() { reader.Close() })
	userIDs := []string{"oauth19-owner", "oauth19-member", "oauth19-foreign"}
	connectorIDs := []string{"oauth19-drive", "oauth19-gmail", "oauth19-foreign"}
	t.Cleanup(func() {
		for _, fixture := range []struct {
			model any
			table string
			ids   []string
		}{
			{&entity.Connector{}, "t_ai_connectors", connectorIDs}, {&entity.UserTenant{}, "t_ai_user_tenants", []string{"oauth19-member"}}, {&entity.User{}, "t_ai_users", userIDs},
		} {
			if err := setup.Where("id IN ?", fixture.ids).Delete(fixture.model).Error; err != nil {
				t.Error("precise OAuth SQL cleanup failed", fixture.table)
			}
			var count int
			if err := reader.QueryRow("SELECT count(*) FROM " + fixture.table + " WHERE id LIKE 'oauth19-%'").Scan(&count); err != nil || count != 0 {
				t.Error("independent OAuth SQL cleanup failed", fixture.table)
			}
			j.Event("sql-cleanup-readback", map[string]any{"table": fixture.table, "ids": fixture.ids, "remaining": count})
		}
	})
	for _, table := range []string{"t_ai_users", "t_ai_user_tenants", "t_ai_connectors"} {
		var count int
		if reader.QueryRow("SELECT count(*) FROM "+table+" WHERE id LIKE 'oauth19-%'").Scan(&count) != nil || count != 0 {
			t.Fatal("OAuth SQL fixture identities already exist")
		}
	}
	active := "1"
	for _, id := range userIDs {
		if err := setup.Create(&entity.User{ID: id, Email: id + "@example.invalid", Nickname: id, Status: &active, IsActive: true, IsAuthenticated: true}).Error; err != nil {
			t.Fatal("OAuth user fixture creation failed")
		}
	}
	if err := setup.Create(&entity.UserTenant{ID: "oauth19-member", UserID: "oauth19-member", TenantID: "oauth19-owner", Role: "normal", Status: &active, InvitedBy: "oauth19-owner"}).Error; err != nil {
		t.Fatal("OAuth membership fixture creation failed")
	}
	for _, fixture := range []struct{ id, tenant, source string }{{"oauth19-drive", "oauth19-owner", "google_drive"}, {"oauth19-gmail", "oauth19-owner", "gmail"}, {"oauth19-foreign", "oauth19-foreign", "google_drive"}} {
		if err := setup.Create(&entity.Connector{ID: fixture.id, TenantID: fixture.tenant, Name: fixture.id, Source: fixture.source, InputType: "poll", Config: entity.JSONMap{"never_expose": "SQL-secret"}, Status: "0"}).Error; err != nil {
			t.Fatal("OAuth connector fixture creation failed")
		}
	}
	readOnlyURL, _ := url.Parse(dsn)
	query := readOnlyURL.Query()
	query.Set("default_transaction_read_only", "on")
	readOnlyURL.RawQuery = query.Encode()
	runtimeDB, err := gorm.Open(postgres.Open(readOnlyURL.String()), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("read-only OAuth SQL unavailable")
	}
	runtimePool, err := runtimeDB.DB()
	if err != nil {
		t.Fatal("read-only OAuth SQL pool unavailable")
	}
	t.Cleanup(func() { runtimePool.Close() })
	previousDB := dao.DB
	dao.DB = runtimeDB
	t.Cleanup(func() { dao.DB = previousDB })
	var failConnectorSQL atomic.Bool
	j.Event("reserve-sql-query-fault", map[string]any{"callback": "oauth19-connector-query-fault", "table": "t_ai_connectors", "effect": "fail before query without SQL mutation"})
	if err := runtimeDB.Callback().Query().Before("gorm:query").Register("oauth19-connector-query-fault", func(tx *gorm.DB) {
		if failConnectorSQL.Load() && tx.Statement.Table == "t_ai_connectors" {
			tx.AddError(errors.New("private-SQL-failure"))
		}
	}); err != nil {
		t.Fatal("OAuth SQL fault registration failed")
	}
	t.Cleanup(func() {
		if err := runtimeDB.Callback().Query().Remove("oauth19-connector-query-fault"); err != nil {
			t.Error("OAuth SQL fault cleanup failed")
		}
	})
	snapshot := func() string {
		t.Helper()
		var state string
		if reader.QueryRow(`SELECT jsonb_build_object('users',(SELECT jsonb_agg(to_jsonb(x) ORDER BY id) FROM t_ai_users x),'members',(SELECT jsonb_agg(to_jsonb(x) ORDER BY id) FROM t_ai_user_tenants x),'connectors',(SELECT jsonb_agg(to_jsonb(x) ORDER BY id) FROM t_ai_connectors x))::text`).Scan(&state) != nil {
			t.Fatal("independent OAuth SQL snapshot failed")
		}
		digest := sha256.Sum256([]byte(state))
		return hex.EncodeToString(digest[:])
	}
	before := snapshot()
	redisOptions := &redis.Options{Addr: net.JoinHostPort(cfg.Redis.Host, strconv.Itoa(cfg.Redis.Port)), Password: cfg.Redis.Password, DB: cfg.Redis.DB}
	j.Event("reserve-redis-client", map[string]any{"endpoint": redisOptions.Addr, "database": cfg.Redis.DB, "key_ownership": "exact SetNX keys registered before mutation"})
	redisClient := redis.NewClient(redisOptions)
	t.Cleanup(func() { redisClient.Close() })
	redisReader := redis.NewClient(redisOptions)
	t.Cleanup(func() { redisReader.Close() })
	if err := redisClient.Ping(ctx).Err(); err != nil {
		t.Fatal("scratch Redis unavailable")
	}
	registered := &connectorOAuthRegisteredStore{store: service.NewConnectorOAuthRedisStore(redisClient), journal: j, keys: map[string]bool{}}
	t.Cleanup(func() {
		registered.mu.Lock()
		defer registered.mu.Unlock()
		for key := range registered.keys {
			if err := redisClient.Del(ctx, key).Err(); err != nil {
				t.Error("precise OAuth Redis cleanup failed")
			}
			count, err := redisReader.Exists(ctx, key).Result()
			if err != nil || count != 0 {
				t.Error("independent OAuth Redis cleanup failed")
			}
			j.Event("redis-cleanup-readback", map[string]any{"endpoint": redisOptions.Addr, "database": cfg.Redis.DB, "key": key, "remaining": count})
		}
	})
	var calls atomic.Int64
	var mode atomic.Int64
	var challenges sync.Map
	const providerAddress = "127.0.0.1:43852"
	j.Event("reserve-tls-listener", map[string]any{"address": providerAddress, "protocol": "https", "purpose": "controlled Google token exchange"})
	listener, err := net.Listen("tcp", providerAddress)
	if err != nil {
		t.Fatal("registered OAuth TLS listener unavailable")
	}
	provider := &httptest.Server{Listener: listener, Config: &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		calls.Add(1)
		if req.TLS == nil || req.Host != "oauth2.googleapis.com" || req.URL.Path != "/token" || req.Method != http.MethodPost {
			t.Error("token exchange did not use fixed Google TLS endpoint")
		}
		if req.ParseForm() != nil {
			t.Error("invalid token request form")
		}
		challenge, ok := challenges.Load(req.PostForm.Get("code"))
		digest := sha256.Sum256([]byte(req.PostForm.Get("code_verifier")))
		if !ok || challenge != base64.RawURLEncoding.EncodeToString(digest[:]) || req.PostForm.Get("client_secret") != "oauth19-client-secret" || req.PostForm.Get("grant_type") != "authorization_code" {
			t.Error("token request lost user flow PKCE/client binding")
		}
		if mode.Load() == 1 {
			w.WriteHeader(http.StatusBadRequest)
			io.WriteString(w, "never-expose-provider-secret")
			return
		}
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, `{"access_token":"oauth19-access","refresh_token":"oauth19-refresh","token_type":"Bearer","expires_in":0}`)
	})}}
	provider.StartTLS()
	t.Cleanup(func() {
		provider.Close()
		probe, err := net.DialTimeout("tcp", providerAddress, time.Second)
		if probe != nil {
			probe.Close()
		}
		j.Event("tls-listener-cleanup-readback", map[string]any{"address": providerAddress, "absent": err != nil})
		if err == nil {
			t.Error("OAuth TLS listener survived cleanup")
		}
	})
	providerURL, _ := url.Parse(provider.URL)
	transport := provider.Client().Transport.(*http.Transport).Clone()
	t.Cleanup(transport.CloseIdleConnections)
	oauth := service.NewConnectorOAuthService(registered, service.ConnectorOAuthConfig{
		GoogleDriveRedirectURI: "http://127.0.0.1:43853/api/v1/connectors/google-drive/oauth/web/callback",
		GmailRedirectURI:       "http://127.0.0.1:43853/api/v1/connectors/gmail/oauth/web/callback",
		PopupOrigin:            "http://127.0.0.1:43853",
		HTTPClient:             &http.Client{Transport: connectorOAuthTLSMapper{transport: transport, endpoint: providerURL}},
	})
	connectors := service.NewConnectorService()
	connectors.SetConnectorOAuthService(oauth)
	credentials := json.RawMessage(`{"web":{"client_id":"oauth19-client","client_secret":"oauth19-client-secret"}}`)
	start := func(actor, source, connector string) *service.StartGoogleWebOAuthResponse {
		t.Helper()
		flow, code, err := connectors.StartGoogleWebOAuth(ctx, actor, source, &service.StartGoogleWebOAuthRequest{Credentials: credentials, ConnectorID: connector})
		if err != nil || code != common.CodeSuccess {
			t.Fatal("authorized SQL OAuth start failed")
		}
		target, _ := url.Parse(flow.AuthorizationURL)
		challenges.Store("code-"+flow.FlowID, target.Query().Get("code_challenge"))
		ttl, err := redisReader.TTL(ctx, service.ConnectorOAuthStateKey(source, flow.FlowID)).Result()
		if err != nil || ttl <= 0 || ttl > 15*time.Minute {
			t.Fatal("state TTL was not persisted")
		}
		return flow
	}
	for _, denial := range []struct{ actor, source, connector string }{{"oauth19-foreign", "google-drive", "oauth19-drive"}, {"oauth19-owner", "gmail", "oauth19-drive"}, {"oauth19-owner", "google-drive", "oauth19-foreign"}} {
		registered.mu.Lock()
		count := len(registered.keys)
		registered.mu.Unlock()
		if _, code, err := connectors.StartGoogleWebOAuth(ctx, denial.actor, denial.source, &service.StartGoogleWebOAuthRequest{Credentials: credentials, ConnectorID: denial.connector}); err == nil || code != common.CodePermissionError {
			t.Fatal("SQL connector denial accepted")
		}
		registered.mu.Lock()
		afterCount := len(registered.keys)
		registered.mu.Unlock()
		if afterCount != count || calls.Load() != 0 {
			t.Fatal("SQL denial created Redis/provider side effects")
		}
	}
	for _, source := range []string{"google-drive", "gmail"} {
		connector := "oauth19-drive"
		if source == "gmail" {
			connector = "oauth19-gmail"
		}
		flow := start("oauth19-owner", source, connector)
		previousCalls := calls.Load()
		var wait sync.WaitGroup
		var successes atomic.Int64
		for range 8 {
			wait.Add(1)
			go func() {
				defer wait.Done()
				html := connectors.GoogleWebOAuthCallback(ctx, source, flow.FlowID, "", "code-"+flow.FlowID)
				if strings.Contains(html, `"status":"success"`) {
					successes.Add(1)
				}
				for _, secret := range []string{"oauth19-access", "oauth19-refresh", "oauth19-client-secret", "code-" + flow.FlowID} {
					if strings.Contains(html, secret) {
						t.Error("OAuth popup leaked secret")
					}
				}
			}()
		}
		wait.Wait()
		if successes.Load() != 1 || calls.Load()-previousCalls != 1 {
			t.Fatal("real Redis callback state was not consumed once")
		}
		if count, err := redisReader.Exists(ctx, service.ConnectorOAuthStateKey(source, flow.FlowID)).Result(); err != nil || count != 0 {
			t.Fatal("independent Redis state readback failed")
		}
		resultKey := service.ConnectorOAuthResultKey(source, flow.FlowID)
		if _, code, err := connectors.PollGoogleWebOAuthResult(ctx, "oauth19-foreign", source, &service.PollGoogleWebOAuthResultRequest{FlowID: flow.FlowID}); err == nil || code != common.CodePermissionError {
			t.Fatal("real Redis exposed another user's result")
		}
		if count, err := redisReader.Exists(ctx, resultKey).Result(); err != nil || count != 1 {
			t.Fatal("cross-account poll destroyed owner result")
		}
		successes.Store(0)
		for range 8 {
			wait.Add(1)
			go func() {
				defer wait.Done()
				result, code, err := connectors.PollGoogleWebOAuthResult(ctx, "oauth19-owner", source, &service.PollGoogleWebOAuthResultRequest{FlowID: flow.FlowID, ConnectorID: connector})
				if err == nil && code == common.CodeSuccess && strings.Contains(result.Credentials, "oauth19-access") {
					successes.Add(1)
				}
			}()
		}
		wait.Wait()
		if successes.Load() != 1 {
			t.Fatal("real Redis result was not consumed once")
		}
		if count, err := redisReader.Exists(ctx, resultKey).Result(); err != nil || count != 0 {
			t.Fatal("independent Redis result readback failed")
		}
		j.Event("oauth-real-boundaries-readback", map[string]any{"source": source, "provider_calls": calls.Load() - previousCalls, "successful_callbacks": 1, "successful_polls": successes.Load(), "state_absent": true, "result_absent": true})
	}
	// The result must survive both a real SQL read failure after Redis peek and
	// cancellation between peek and authorization. A later retry still consumes
	// exactly the version it authorized.
	retryFlow := start("oauth19-owner", "google-drive", "oauth19-drive")
	if html := connectors.GoogleWebOAuthCallback(ctx, "google-drive", retryFlow.FlowID, "", "code-"+retryFlow.FlowID); !strings.Contains(html, `"status":"success"`) {
		t.Fatal("retry result fixture exchange failed")
	}
	retryKey := service.ConnectorOAuthResultKey("google-drive", retryFlow.FlowID)
	originalRaw, err := redisReader.Get(ctx, retryKey).Result()
	if err != nil || originalRaw == "" {
		t.Fatal("independent retry result readback failed")
	}
	failConnectorSQL.Store(true)
	result, code, pollErr := connectors.PollGoogleWebOAuthResult(ctx, "oauth19-owner", "google-drive", &service.PollGoogleWebOAuthResultRequest{FlowID: retryFlow.FlowID})
	failConnectorSQL.Store(false)
	if result != nil || code != common.CodeServerError || pollErr == nil || strings.Contains(pollErr.Error(), "private-") {
		t.Fatal("transient SQL poll failure lost classification or leaked credentials")
	}
	if current, err := redisReader.Get(ctx, retryKey).Result(); err != nil || current != originalRaw {
		t.Fatal("transient SQL failure destroyed retryable OAuth result")
	}
	cancelledCtx, cancelPoll := context.WithCancel(ctx)
	registered.mu.Lock()
	registered.afterPeek = cancelPoll
	registered.mu.Unlock()
	result, code, pollErr = connectors.PollGoogleWebOAuthResult(cancelledCtx, "oauth19-owner", "google-drive", &service.PollGoogleWebOAuthResultRequest{FlowID: retryFlow.FlowID})
	registered.mu.Lock()
	registered.afterPeek = nil
	registered.mu.Unlock()
	cancelPoll()
	if result != nil || code != common.CodeServerError || pollErr == nil {
		t.Fatal("cancelled poll returned credentials or permission denial")
	}
	if current, err := redisReader.Get(ctx, retryKey).Result(); err != nil || current != originalRaw {
		t.Fatal("cancelled poll destroyed retryable OAuth result")
	}
	j.Event("reserve-redis-result-version-replacement", map[string]any{"endpoint": redisOptions.Addr, "database": cfg.Redis.DB, "key": retryKey, "change": "append JSON whitespace to prove exact byte CAS"})
	replacement := originalRaw + " "
	if err := redisClient.Set(ctx, retryKey, replacement, 15*time.Minute).Err(); err != nil {
		t.Fatal("CAS replacement fixture failed")
	}
	if consumed, err := registered.ConsumeOwned(ctx, retryKey, originalRaw, "oauth19-owner", "google-drive", "oauth19-drive"); err != nil || consumed {
		t.Fatal("Redis CAS consumed a replaced result version")
	}
	if current, err := redisReader.Get(ctx, retryKey).Result(); err != nil || current != replacement {
		t.Fatal("stale Redis CAS changed replacement result")
	}
	result, code, pollErr = connectors.PollGoogleWebOAuthResult(ctx, "oauth19-owner", "google-drive", &service.PollGoogleWebOAuthResultRequest{FlowID: retryFlow.FlowID})
	if result == nil || code != common.CodeSuccess || pollErr != nil {
		t.Fatal("retry could not consume current authorized result")
	}
	if count, err := redisReader.Exists(ctx, retryKey).Result(); err != nil || count != 0 {
		t.Fatal("successful retry left a reusable result")
	}
	j.Event("oauth-retry-and-cas-independent-readback", map[string]any{"sql_failure_preserved": true, "cancellation_after_peek_preserved": true, "stale_cas_preserved_replacement": true, "authorized_retry_consumed": true})
	// Revocation is an explicit fixture write; the read-only runtime must recheck
	// SQL before exchange and may not reuse start-time membership permission.
	if after := snapshot(); before != after {
		t.Fatal("connector OAuth changed SQL during authorized/denied flows")
	}
	memberResultFlow := start("oauth19-member", "google-drive", "oauth19-drive")
	if html := connectors.GoogleWebOAuthCallback(ctx, "google-drive", memberResultFlow.FlowID, "", "code-"+memberResultFlow.FlowID); !strings.Contains(html, `"status":"success"`) {
		t.Fatal("member result fixture exchange failed")
	}
	memberResultKey := service.ConnectorOAuthResultKey("google-drive", memberResultFlow.FlowID)
	memberRaw, err := redisReader.Get(ctx, memberResultKey).Result()
	if err != nil || memberRaw == "" {
		t.Fatal("member result fixture readback failed")
	}
	flow := start("oauth19-member", "google-drive", "oauth19-drive")
	j.Event("reserve-membership-revocation", map[string]any{"table": "t_ai_user_tenants", "id": "oauth19-member", "status": "0"})
	if err := setup.Model(&entity.UserTenant{}).Where("id = ?", "oauth19-member").Update("status", "0").Error; err != nil {
		t.Fatal("membership revocation fixture failed")
	}
	before = snapshot()
	if result, code, err := connectors.PollGoogleWebOAuthResult(ctx, "oauth19-member", "google-drive", &service.PollGoogleWebOAuthResultRequest{FlowID: memberResultFlow.FlowID}); result != nil || code != common.CodePermissionError || err == nil {
		t.Fatal("revoked membership read OAuth credentials")
	}
	if current, err := redisReader.Get(ctx, memberResultKey).Result(); err != nil || current != memberRaw {
		t.Fatal("denied membership poll modified its protected result")
	}
	previousCalls := calls.Load()
	if html := connectors.GoogleWebOAuthCallback(ctx, "google-drive", flow.FlowID, "", "code-"+flow.FlowID); strings.Contains(html, `"status":"success"`) || calls.Load() != previousCalls {
		t.Fatal("revoked SQL membership exchanged tokens")
	}
	if count, err := redisReader.Exists(ctx, service.ConnectorOAuthStateKey("google-drive", flow.FlowID), service.ConnectorOAuthResultKey("google-drive", flow.FlowID)).Result(); err != nil || count != 0 {
		t.Fatal("revoked flow left usable state/results")
	}
	flow = start("oauth19-owner", "google-drive", "oauth19-drive")
	mode.Store(1)
	if html := connectors.GoogleWebOAuthCallback(ctx, "google-drive", flow.FlowID, "", "code-"+flow.FlowID); strings.Contains(html, `"status":"success"`) || strings.Contains(html, "never-expose") {
		t.Fatal("TLS provider error succeeded or leaked secrets")
	}
	if count, err := redisReader.Exists(ctx, service.ConnectorOAuthStateKey("google-drive", flow.FlowID), service.ConnectorOAuthResultKey("google-drive", flow.FlowID)).Result(); err != nil || count != 0 {
		t.Fatal("provider error retained usable state/results")
	}
	if after := snapshot(); before != after {
		t.Fatal("connector OAuth modified SQL identity or connector state")
	}
	j.Event("oauth-sql-independent-readback", map[string]any{"before_sha256": before, "after_sha256": snapshot(), "runtime_default_transaction_read_only": true, "unchanged": true})
}
