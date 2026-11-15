package service

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"gorm.io/gorm"
	"multirag/internal/common"
	rediscache "multirag/internal/engine/redis"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/testsupport"
)

func TestCanonicalOAuthStateSQLHTTP(t *testing.T) {
	f := canonicalLink(t)
	if err := rediscache.Init(&f.Config.Redis); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { rediscache.Close() })
	store := rediscache.Get()
	svc := NewUserService()
	var calls atomic.Int64
	var email atomic.Value
	email.Store(f.owner + "@fixture.invalid")
	peer := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		switch r.URL.Path {
		case "/token":
			if err := r.ParseForm(); err != nil || r.Form.Get("code") != "code" {
				t.Error("OAuth code payload", err)
			}
			fmt.Fprint(w, `{"access_token":"controlled-access"}`)
		case "/info":
			if r.Header.Get("Authorization") != "Bearer controlled-access" {
				t.Error("userinfo lost token")
			}
			fmt.Fprintf(w, `{"email":%q,"name":"owned OAuth"}`, email.Load().(string))
		default:
			w.WriteHeader(404)
		}
	}))
	cfg := server.GetConfig()
	oldOAuth := cfg.OAuth
	cfg.OAuth = map[string]server.OAuthConfig{"fixture": {Type: "oauth2", ClientID: "client", ClientSecret: "private-client", AuthorizationURL: peer.URL + "/auth", TokenURL: peer.URL + "/token", UserinfoURL: peer.URL + "/info", RedirectURI: "https://app.invalid/callback"}}
	t.Cleanup(func() { cfg.OAuth = oldOAuth })
	if init, _, err := svc.OAuthLoginInitiate("fixture", nil); err == nil || init != nil {
		t.Fatal("nil Redis initiation succeeded", init, err)
	}
	if result, _, err := svc.OAuthCallback(t.Context(), "fixture", "code", "state", "state", nil); err == nil || result != nil || calls.Load() != 0 {
		t.Fatal("nil Redis callback reached supplier", result, err)
	}
	f.Journal.Event("reserve-oauth-state-key-prefix", map[string]any{"prefix": oauthStateKey, "suffix": "original cryptographic random state", "cleanup": "captured exact keys and independent absence check"})
	states := []string{}
	begin := func() string {
		t.Helper()
		init, code, err := svc.OAuthLoginInitiate("fixture", store)
		if err != nil || code != common.CodeSuccess {
			t.Fatal(init, code, err)
		}
		states = append(states, init.State)
		return init.State
	}
	t.Cleanup(func() {
		cleanupCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		for _, state := range states {
			if err := f.Redis.Del(cleanupCtx, oauthStateKey+state).Err(); err != nil {
				t.Error(err)
			}
			if exists := f.Redis.Exists(cleanupCtx, oauthStateKey+state).Val(); exists != 0 {
				t.Error("OAuth state cleanup")
			}
		}
	})
	state := begin()
	before := calls.Load()
	var wg sync.WaitGroup
	successes := make(chan bool, 2)
	for range 2 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			result, code, err := svc.OAuthCallback(t.Context(), "fixture", "code", state, state, store)
			successes <- err == nil && code == common.CodeSuccess && result != nil
		}()
	}
	wg.Wait()
	close(successes)
	count := 0
	for ok := range successes {
		if ok {
			count++
		}
	}
	if count != 1 || calls.Load() != before+2 {
		t.Fatal("OAuth state consumed more than once", count, calls.Load()-before)
	}
	for _, actorState := range []string{"status", "is_active"} {
		if err := f.DB.Model(&entity.User{}).Where("id=?", f.owner).Update(actorState, "0").Error; err != nil {
			t.Fatal(err)
		}
		state = begin()
		if result, _, err := svc.OAuthCallback(t.Context(), "fixture", "code", state, state, store); !errors.Is(err, ErrOAuthUserInactive) || result != nil {
			t.Fatal("inactive OAuth account accepted", actorState, result, err)
		}
		if err := f.DB.Model(&entity.User{}).Where("id=?", f.owner).Update(actorState, "1").Error; err != nil {
			t.Fatal(err)
		}
	}
	email.Store(f.Prefix + "new@fixture.invalid")
	failure := errors.New("controlled root-file SQL rejection")
	if err := f.DB.Callback().Create().Before("gorm:create").Register("oauth-bootstrap-fail", func(tx *gorm.DB) {
		if tx.Statement.Schema != nil && tx.Statement.Schema.Name == "File" {
			tx.AddError(failure)
		}
	}); err != nil {
		t.Fatal(err)
	}
	state = begin()
	if result, _, err := svc.OAuthCallback(t.Context(), "fixture", "code", state, state, store); err == nil || result != nil {
		t.Fatal("failed OAuth bootstrap succeeded", result, err)
	}
	f.DB.Callback().Create().Remove("oauth-bootstrap-fail")
	var rows int
	if err := f.Read.QueryRow("SELECT count(*) FROM user WHERE email=?", email.Load().(string)).Scan(&rows); err != nil || rows != 0 {
		t.Fatal("failed OAuth bootstrap left user", rows, err)
	}
	state = begin()
	created, code, err := svc.OAuthCallback(t.Context(), "fixture", "code", state, state, store)
	if err != nil || code != common.CodeSuccess || created == nil || !created.IsNewUser {
		t.Fatal("original OAuth bootstrap", created, code, err)
	}
	if err := f.Read.QueryRow("SELECT count(*) FROM user u JOIN tenant t ON t.id=u.id JOIN user_tenant ut ON ut.user_id=u.id AND ut.tenant_id=t.id JOIN file f ON f.tenant_id=t.id WHERE u.email=? AND ut.role='owner' AND t.status='1'", email.Load().(string)).Scan(&rows); err != nil || rows != 1 {
		t.Fatal("independent OAuth bootstrap readback", rows, err)
	}
	f.Journal.Event("canonical-oauth-state-sql-readback", map[string]any{"real_redis_atomic_one_time_state": true, "controlled_oauth_http": true, "nil_state_store_denied": true, "inactive_accounts_denied": true, "bootstrap_sql_rollback_and_success": true, "oidc_jwt_nonce_validation": "not implemented in frozen target; not claimed"})
}
