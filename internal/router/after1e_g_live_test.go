package router

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v4"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/cache"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/testsupport"
	"multirag/internal/utility"
)

// TestAfter1eGMCPAndOAuth exercises registered routes, real SQL/Redis, and a
// controlled TLS provider. Provider profiles never become platform accounts.
func TestAfter1eGMCPAndOAuth(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_AFTER1E_G_DSN")
	if dsn == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("scratch SQL required")
		}
		t.Skip("requires owned scratch SQL and Redis database")
	}
	u, err := url.Parse(dsn)
	if err != nil || !strings.HasPrefix(strings.TrimPrefix(u.Path, "/"), "multirag_after1e_g_") {
		t.Fatal("unowned SQL")
	}
	j := testsupport.NewCase(t)
	ctx := context.Background()
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	cfgPath := filepath.Join(os.Getenv("TMPDIR"), "after1e-g.yaml")
	j.Event("reserve-config", map[string]any{"path": cfgPath})
	if err := os.WriteFile(cfgPath, []byte("multirag:\n  secret_key: after1e-g-owned-secret-32-characters\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(cfgPath); err != nil {
		t.Fatal(err)
	}
	if err := server.InitVariables(nil); err != nil {
		t.Fatal(err)
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("SQL connect")
	}
	pool, _ := db.DB()
	t.Cleanup(func() { pool.Close() })
	reader, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("SQL reader")
	}
	t.Cleanup(func() { reader.Close() })
	old := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = old })
	j.Event("reserve-sql-schema", map[string]any{"database": u.Path, "tables": []string{"t_ai_users", "t_ai_tenants", "t_ai_user_tenants", "t_ai_api_tokens", "t_ai_mcp_server"}})
	if err := db.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.APIToken{}, &entity.MCPServer{}); err != nil {
		t.Fatal(err)
	}
	j.Event("reserve-sql-identities", map[string]any{"users": []string{"owner", "member", "foreign", "revoked", "invite"}, "servers": []string{"a", "b", "c", "d", "foreign"}})
	active, inactive, session := "1", "0", "existing-token"
	for _, id := range []string{"owner", "member", "foreign", "revoked", "invite"} {
		if err := db.Create(&entity.User{ID: id, Email: id + "@after1e.invalid", Nickname: id, AccessToken: &session, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		tenant, role, status := "owner", "normal", active
		if id == "owner" {
			role = "owner"
		}
		if id == "foreign" {
			tenant, role = id, "owner"
		}
		if id == "revoked" {
			status = inactive
		}
		if id == "invite" {
			role = "invite"
		}
		if err := db.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: tenant, Role: role, Status: &status}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, id := range []string{"owner", "foreign"} {
		if err := db.Create(&entity.Tenant{ID: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	stamp := int64(42)
	date := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	for _, id := range []string{"a", "b", "c", "d", "foreign"} {
		tenant := "owner"
		if id == "foreign" {
			tenant = "foreign"
		}
		name := "Report_100%"
		if id == "d" {
			name = "Other"
		}
		if err := db.Create(&entity.MCPServer{ID: id, TenantID: tenant, Name: name, ServerType: "sse", URL: "https://example.invalid/mcp?token=secret-query", Headers: entity.JSONMap{"Authorization": "secret-header"}, Variables: entity.JSONMap{"authorization_token": "secret-variable", "nested": map[string]any{"password": "secret-nested"}, "tools": map[string]any{"display": map[string]any{"description": "cached metadata", "secret": "secret-tool", "headers": map[string]any{"Authorization": "secret-tool-header"}, "config": map[string]any{"api_key": "secret-tool-nested"}}}}, BaseModel: entity.BaseModel{CreateTime: &stamp, CreateDate: &date}}).Error; err != nil {
			t.Fatal(err)
		}
	}
	oldAdmin := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned")
	t.Cleanup(func() { local.SetAdminStatus(oldAdmin.Status, oldAdmin.Reason) })
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	(&Router{authHandler: handler.NewAuthHandler(), userHandler: handler.NewUserHandler(service.NewUserService()), systemHandler: handler.NewSystemHandler(service.NewSystemService()), documentHandler: handler.NewDocumentHandler(service.NewDocumentService(nil))}).Setup(app)
	startServer := func(addr string, h http.Handler, tls bool) *httptest.Server {
		t.Helper()
		j.Event("reserve-listener", map[string]any{"address": addr})
		l, err := net.Listen("tcp", addr)
		if err != nil {
			t.Fatal(err)
		}
		s := &httptest.Server{Listener: l, Config: &http.Server{Handler: h}}
		if tls {
			s.StartTLS()
		} else {
			s.Start()
		}
		t.Cleanup(func() {
			s.Close()
			probe, err := net.DialTimeout("tcp", addr, time.Second)
			if err == nil {
				probe.Close()
				t.Error("listener remains")
			}
			j.Event("listener-cleanup-readback", map[string]any{"address": addr, "absent": err != nil})
		})
		return s
	}
	api := startServer("127.0.0.1:18781", app, false)
	jwtTokens := map[string]string{}
	for _, id := range []string{"owner", "member", "foreign", "revoked", "invite"} {
		token, err := utility.SignUserJWT(id+"@after1e.invalid", server.GetSecretKey())
		if err != nil {
			t.Fatal(err)
		}
		jwtTokens[id] = token
	}
	request := func(path, user, cookie string) (*http.Response, []byte) {
		t.Helper()
		req, _ := http.NewRequest("GET", api.URL+path, nil)
		if user != "" {
			req.Header.Set("Authorization", "Bearer "+jwtTokens[user])
		}
		if cookie != "" {
			req.AddCookie(&http.Cookie{Name: "multirag_oauth_state", Value: cookie})
		}
		res, err := (&http.Client{CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}).Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		raw, _ := io.ReadAll(res.Body)
		return res, raw
	}
	snapshot := func() string {
		t.Helper()
		var raw string
		err := reader.QueryRow(`SELECT json_build_object('users',(SELECT json_agg(u ORDER BY id) FROM public.t_ai_users u),'tenants',(SELECT json_agg(t ORDER BY id) FROM public.t_ai_tenants t),'members',(SELECT json_agg(m ORDER BY id) FROM public.t_ai_user_tenants m),'tokens',(SELECT json_agg(a ORDER BY token) FROM public.t_ai_api_tokens a),'mcp',(SELECT json_agg(s ORDER BY id) FROM public.t_ai_mcp_server s))::text`).Scan(&raw)
		if err != nil {
			t.Fatal(err)
		}
		return raw
	}
	beforeLists := snapshot()
	list := func(query, user string) map[string]any {
		t.Helper()
		res, raw := request("/api/v1/mcp/servers"+query, user, "")
		if user == "" {
			if res.StatusCode != 401 {
				t.Fatal("missing JWT accepted")
			}
			return nil
		}
		var payload map[string]any
		if json.Unmarshal(raw, &payload) != nil {
			t.Fatal("bad response")
		}
		if strings.Contains(string(raw), "secret-") {
			t.Fatal("credential leaked")
		}
		return payload
	}
	list("", "")
	payload := list("", "owner")
	if payload["code"] != float64(0) || payload["data"].(map[string]any)["total"] != float64(4) {
		t.Fatal("unfiltered list", payload)
	}
	for _, item := range payload["data"].(map[string]any)["mcp_servers"].([]any) {
		variables := item.(map[string]any)["variables"].(map[string]any)
		tools, ok := variables["tools"].(map[string]any)
		if !ok || len(variables) != 1 || len(tools) != 0 {
			t.Fatal("list must expose only empty tools")
		}
	}
	var toolSecret, toolHeader, toolNested string
	if err := reader.QueryRow(`SELECT variables->'tools'->'display'->>'secret',variables->'tools'->'display'->'headers'->>'Authorization',variables->'tools'->'display'->'config'->>'api_key' FROM public.t_ai_mcp_server WHERE id='a'`).Scan(&toolSecret, &toolHeader, &toolNested); err != nil {
		t.Fatal(err)
	}
	if toolSecret != "secret-tool" || toolHeader != "secret-tool-header" || toolNested != "secret-tool-nested" {
		t.Fatal("list changed persisted tool credentials")
	}
	j.Event("mcp-tool-secret-independent-readback", map[string]any{"responseEmptyTools": true, "sqlToolSecretHeaderAndNestedConfigUnchanged": true})
	payload = list("?mcp_ids=[]", "owner")
	data := payload["data"].(map[string]any)
	if data["total"] != float64(0) || len(data["mcp_servers"].([]any)) != 0 {
		t.Fatal("empty broadened")
	}
	for _, desc := range []string{"true", "false"} {
		ids := []string{}
		for page := 1; page <= 4; page++ {
			p := list(fmt.Sprintf("?page=%d&page_size=1&orderby=create_time&desc=%s", page, desc), "owner")
			data := p["data"].(map[string]any)
			ids = append(ids, data["mcp_servers"].([]any)[0].(map[string]any)["id"].(string))
		}
		want := "abcd"
		if desc == "true" {
			want = "dcba"
		}
		if strings.Join(ids, "") != want {
			t.Fatal("unstable pages", ids)
		}
	}
	for _, keyword := range []string{"%", "_", "Report_100%"} {
		data := list("?keywords="+url.QueryEscape(keyword), "owner")["data"].(map[string]any)
		if data["total"] != float64(3) {
			t.Fatal("keyword wildcard broadened search", keyword, data)
		}
	}
	if list("?mcp_ids=a,b", "owner")["data"].(map[string]any)["total"] != float64(2) {
		t.Fatal("ID filter not applied")
	}
	for _, raw := range []string{"?page=-1", "?page_size=101", "?desc=no", "?orderby=bad", "?mcp_ids=a,a", "?mcp_ids=&mcp_id=a"} {
		if list(raw, "owner")["code"] == float64(0) {
			t.Fatal("malformed accepted", raw)
		}
	}
	if list("?mcp_ids=foreign", "owner")["data"].(map[string]any)["total"] != float64(0) {
		t.Fatal("foreign id leaked")
	}
	for _, id := range []string{"member", "revoked", "invite", "foreign"} {
		if list("?tenant_id=owner", id)["code"] == float64(0) {
			t.Fatal("unauthorized manager", id)
		}
	}
	if snapshot() != beforeLists {
		t.Fatal("MCP list or denial wrote SQL")
	}
	j.Event("mcp-independent-readback", map[string]any{"allSQLUnchanged": true})
	// Ownership alone is insufficient after membership or tenant revocation.
	for _, changes := range []map[string]any{{"role": "normal"}, {"role": "invite"}, {"status": "0"}} {
		if err := db.Model(&entity.UserTenant{}).Where("id = ?", "owner").Updates(changes).Error; err != nil {
			t.Fatal(err)
		}
		if list("", "owner")["code"] == float64(0) {
			t.Fatal("revoked owner accepted")
		}
		if err := db.Model(&entity.UserTenant{}).Where("id = ?", "owner").Updates(map[string]any{"role": "owner", "status": "1"}).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := db.Model(&entity.Tenant{}).Where("id = ?", "owner").Update("status", "0").Error; err != nil {
		t.Fatal(err)
	}
	if list("", "owner")["code"] == float64(0) {
		t.Fatal("inactive tenant accepted")
	}
	if err := db.Model(&entity.Tenant{}).Where("id = ?", "owner").Update("status", "1").Error; err != nil {
		t.Fatal(err)
	}
	// SQL reader takes a full before/after snapshot, including login tokens.
	before := snapshot()
	// No Redis means no state bypass and no provider request.
	config := server.GetConfig()
	config.OAuth = map[string]server.OAuthConfig{"test": {Type: "oauth2", ClientID: "client", AuthorizationURL: "https://example.invalid/auth", TokenURL: "https://example.invalid/token", UserinfoURL: "https://example.invalid/user", RedirectURI: "https://app.invalid/callback"}}
	res, _ := request("/api/v1/auth/login/test", "", "")
	if res.Header.Get("Location") != "/?error=oauth_unavailable" {
		t.Fatal("nil Redis bypass")
	}
	var redisCfg server.RedisConfig
	if json.Unmarshal([]byte(os.Getenv("MULTIRAG_AFTER1E_G_REDIS")), &redisCfg) != nil || redisCfg.DB != 15 {
		t.Fatal("owned Redis DB required")
	}
	j.Event("reserve-redis-database", map[string]any{"host": redisCfg.Host, "port": redisCfg.Port, "db": 15, "prefix": "oauth:state:", "requireEmpty": true})
	if err := cache.Init(&redisCfg); err != nil {
		t.Fatal("Redis connect")
	}
	store := cache.Get().GetClient()
	t.Cleanup(func() { cache.Close() })
	if count, err := store.DBSize(ctx).Result(); err != nil || count != 0 {
		t.Fatal("Redis database not empty")
	}
	states := []string{}
	t.Cleanup(func() {
		for _, state := range states {
			if err := store.Del(ctx, "oauth:state:"+state).Err(); err != nil {
				t.Error("Redis cleanup")
			}
		}
		count, err := store.DBSize(ctx).Result()
		if err != nil || count != 0 {
			t.Error("Redis cleanup readback")
		}
		j.Event("redis-cleanup-readback", map[string]any{"database": 15, "remaining": count})
	})
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	var providerCalls atomic.Int64
	var mode, nonce string
	var mu sync.Mutex
	providerURL := ""
	provider := startServer("127.0.0.1:18782", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		providerCalls.Add(1)
		mu.Lock()
		defer mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/.well-known/openid-configuration":
			json.NewEncoder(w).Encode(map[string]string{"issuer": providerURL, "authorization_endpoint": providerURL + "/auth", "token_endpoint": providerURL + "/token", "userinfo_endpoint": providerURL + "/userinfo", "jwks_uri": providerURL + "/keys"})
		case "/token":
			r.ParseForm()
			if len(r.Form.Get("code_verifier")) != 64 {
				t.Error("PKCE verifier absent")
			}
			if mode == "error" {
				w.WriteHeader(400)
				io.WriteString(w, "secret-provider-body")
				return
			}
			claims := jwt.MapClaims{"iss": providerURL, "sub": "subject", "aud": "client", "exp": time.Now().Add(time.Hour).Unix(), "iat": time.Now().Unix(), "nonce": nonce}
			if mode == "bad-nonce" {
				claims["nonce"] = "attacker"
			}
			token := jwt.NewWithClaims(jwt.SigningMethodRS256, claims)
			token.Header["kid"] = "key"
			signed, _ := token.SignedString(key)
			json.NewEncoder(w).Encode(map[string]string{"access_token": "provider-token", "token_type": "Bearer", "id_token": signed})
		case "/keys":
			json.NewEncoder(w).Encode(map[string]any{"keys": []map[string]string{{"kid": "key", "kty": "RSA", "n": base64.RawURLEncoding.EncodeToString(key.N.Bytes()), "e": "AQAB"}}})
		case "/userinfo":
			if r.Header.Get("Authorization") != "Bearer provider-token" {
				t.Error("missing provider bearer")
			}
			io.WriteString(w, `{"sub":"subject","email":"owner@after1e.invalid"}`)
		default:
			t.Error("unexpected provider route")
		}
	}), true)
	providerURL = provider.URL
	oldTransport := http.DefaultTransport
	http.DefaultTransport = provider.Client().Transport
	t.Cleanup(func() { http.DefaultTransport = oldTransport })
	cfg := config.OAuth["test"]
	cfg.AuthorizationURL = providerURL + "/auth"
	cfg.TokenURL = providerURL + "/token"
	cfg.UserinfoURL = providerURL + "/userinfo"
	config.OAuth["test"] = cfg
	oidc := cfg
	oidc.Type = "oidc"
	oidc.Issuer = providerURL
	config.OAuth["oidc"] = oidc
	begin := func(channel string) (string, string) {
		t.Helper()
		res, _ := request("/api/v1/auth/login/"+channel, "", "")
		if res.StatusCode != 302 {
			t.Fatal("start failed")
		}
		target, err := url.Parse(res.Header.Get("Location"))
		if err != nil {
			t.Fatal(err)
		}
		state := target.Query().Get("state")
		if len(state) == 64 {
			states = append(states, state)
		}
		if len(state) != 64 || target.Query().Get("code_challenge_method") != "S256" {
			t.Fatal("missing state/PKCE")
		}
		j.Event("created-redis-key", map[string]any{"database": 15, "key": "oauth:state:" + state})
		cookies := res.Cookies()
		if len(cookies) != 1 || !cookies[0].HttpOnly || !cookies[0].Secure || cookies[0].SameSite != http.SameSiteLaxMode {
			t.Fatal("unsafe state cookie")
		}
		return state, target.Query().Get("nonce")
	}
	callback := func(channel, state, cookie string) string {
		res, raw := request("/api/v1/auth/oauth/"+channel+"/callback?code=code&state="+state, "", cookie)
		if strings.Contains(string(raw), "secret-provider") || res.Header.Get("Authorization") != "" {
			t.Fatal("provider/auth leak")
		}
		for _, c := range res.Cookies() {
			if c.Name != "multirag_oauth_state" || c.MaxAge != -1 {
				t.Fatal("issued auth cookie")
			}
		}
		return res.Header.Get("Location")
	}
	for _, channel := range []string{"test", "oidc"} {
		state, n := begin(channel)
		mu.Lock()
		nonce, mode = n, "valid"
		mu.Unlock()
		if callback(channel, state, state) != "/?error=oauth_identity_binding_required" {
			t.Fatal("platform identity enabled or valid protocol refused")
		}
		calls := providerCalls.Load()
		if callback(channel, state, state) != "/?error=invalid_state" || providerCalls.Load() != calls {
			t.Fatal("replay reached provider")
		}
	}
	state, _ := begin("test")
	calls := providerCalls.Load()
	if callback("test", state, strings.Repeat("0", 64)) != "/?error=invalid_state" || providerCalls.Load() != calls {
		t.Fatal("CSRF reached provider")
	}
	state, n := begin("oidc")
	mu.Lock()
	nonce, mode = n, "bad-nonce"
	mu.Unlock()
	if callback("oidc", state, state) != "/?error=oauth_protocol_error" {
		t.Fatal("bad OIDC accepted")
	}
	state, _ = begin("test")
	mu.Lock()
	mode = "error"
	mu.Unlock()
	if callback("test", state, state) != "/?error=oauth_protocol_error" {
		t.Fatal("provider error leak")
	}
	state, _ = begin("test")
	calls = providerCalls.Load()
	res, _ = request("/api/v1/auth/oauth/test/callback?state="+state, "", state)
	if res.Header.Get("Location") != "/?error=missing_code" || providerCalls.Load() != calls {
		t.Fatal("missing code reached provider")
	}
	if callback("test", state, state) != "/?error=invalid_state" {
		t.Fatal("missing-code state reused")
	}
	state, _ = begin("test")
	store.Del(ctx, "oauth:state:"+state)
	if callback("test", state, state) != "/?error=invalid_state" {
		t.Fatal("expired state accepted")
	}
	state, _ = begin("test")
	calls = providerCalls.Load()
	if callback("oidc", state, state) != "/?error=invalid_state" || providerCalls.Load() != calls {
		t.Fatal("cross-channel state accepted")
	}
	state, _ = begin("test")
	mu.Lock()
	mode = "valid"
	mu.Unlock()
	var successes atomic.Int64
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			err := service.NewUserService().OAuthCallback(ctx, "test", "code", state, state, cache.Get())
			if err == service.ErrOAuthIdentityBindingRequired {
				successes.Add(1)
			} else if err != service.ErrOAuthInvalidState {
				t.Error("unexpected replay outcome", err)
			}
		}()
	}
	wg.Wait()
	if successes.Load() != 1 {
		t.Fatal("state consumed more than once")
	}
	if snapshot() != before {
		t.Fatal("OAuth rejection or list changed SQL")
	}
	j.Event("sql-independent-readback", map[string]any{"accountTenantTokenAndMCPUnchanged": true, "protocolSuccessStillDenied": true, "atomicReplayConsumers": 1})
}
