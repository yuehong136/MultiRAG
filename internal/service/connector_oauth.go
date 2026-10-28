package service

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"slices"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
	"multirag/internal/cache"
	"multirag/internal/common"
)

const (
	connectorOAuthTTL       = 15 * time.Minute
	connectorOAuthTimeout   = 7 * time.Second
	connectorGoogleAuthURL  = "https://accounts.google.com/o/oauth2/v2/auth"
	connectorGoogleTokenURL = "https://oauth2.googleapis.com/token"
	connectorOAuthLimit     = 1 << 20
)

var (
	errConnectorOAuthMissing     = errors.New("authorization is still pending")
	errConnectorOAuthForbidden   = errors.New("authorization result is not available to this user")
	errConnectorOAuthUnavailable = errors.New("connector authorization is unavailable")
	errConnectorOAuthProtocol    = errors.New("unable to complete Google authorization")
)

type StartGoogleWebOAuthRequest struct {
	Credentials json.RawMessage `json:"credentials"`
	RedirectURI string          `json:"redirect_uri,omitempty"`
	ConnectorID string          `json:"connector_id,omitempty"`
}

type StartGoogleWebOAuthResponse struct {
	FlowID           string `json:"flow_id"`
	AuthorizationURL string `json:"authorization_url"`
	ExpiresIn        int64  `json:"expires_in"`
}

type PollGoogleWebOAuthResultRequest struct {
	FlowID      string `json:"flow_id"`
	ConnectorID string `json:"connector_id,omitempty"`
}

type PollGoogleWebOAuthResultResponse struct {
	Credentials string `json:"credentials"`
}

// ConnectorOAuthStore consumes state atomically. Results are read under their
// ownership guard, then consumed by CAS after SQL authorization succeeds.
// Neither an ownership denial nor a failed CAS may delete the result.
type ConnectorOAuthStore interface {
	SetNX(context.Context, string, string, time.Duration) (bool, error)
	Take(context.Context, string) (string, error)
	PeekOwned(context.Context, string, string, string, string) (string, error)
	ConsumeOwned(context.Context, string, string, string, string, string) (bool, error)
}

type connectorOAuthRedisStore struct{ client *redis.Client }

func NewConnectorOAuthRedisStore(client *redis.Client) ConnectorOAuthStore {
	return &connectorOAuthRedisStore{client: client}
}

func (s *connectorOAuthRedisStore) SetNX(ctx context.Context, key, value string, ttl time.Duration) (bool, error) {
	if s.client == nil {
		return false, errConnectorOAuthUnavailable
	}
	return s.client.SetNX(ctx, key, value, ttl).Result()
}

func (s *connectorOAuthRedisStore) Take(ctx context.Context, key string) (string, error) {
	if s.client == nil {
		return "", errConnectorOAuthUnavailable
	}
	value, err := s.client.GetDel(ctx, key).Result()
	if errors.Is(err, redis.Nil) {
		return "", errConnectorOAuthMissing
	}
	return value, err
}

func (s *connectorOAuthRedisStore) PeekOwned(ctx context.Context, key, userID, source, connectorID string) (string, error) {
	if s.client == nil {
		return "", errConnectorOAuthUnavailable
	}
	const script = `local raw=redis.call('GET',KEYS[1]); if not raw then return false end; local ok,v=pcall(cjson.decode,raw); if not ok or type(v)~='table' then return '__invalid__' end; if v.user_id~=ARGV[1] or v.source~=ARGV[2] or (ARGV[3]~='' and v.connector_id~=ARGV[3]) then return '__forbidden__' end; return raw`
	value, err := s.client.Eval(ctx, script, []string{key}, userID, source, connectorID).Text()
	if errors.Is(err, redis.Nil) {
		return "", errConnectorOAuthMissing
	}
	if err != nil {
		return "", err
	}
	switch value {
	case "__forbidden__":
		return "", errConnectorOAuthForbidden
	case "__invalid__":
		return "", errConnectorOAuthProtocol
	default:
		return value, nil
	}
}

func (s *connectorOAuthRedisStore) ConsumeOwned(ctx context.Context, key, rawExpected, userID, source, connectorID string) (bool, error) {
	if s.client == nil {
		return false, errConnectorOAuthUnavailable
	}
	// Recheck both ownership and the exact bytes read before SQL I/O. A parallel
	// poll, expiration or replacement must never return stale credentials.
	const script = `local raw=redis.call('GET',KEYS[1]); if not raw then return 0 end; local ok,v=pcall(cjson.decode,raw); if not ok or type(v)~='table' then return -2 end; if v.user_id~=ARGV[2] or v.source~=ARGV[3] or (ARGV[4]~='' and v.connector_id~=ARGV[4]) then return -1 end; if raw~=ARGV[1] then return 0 end; redis.call('DEL',KEYS[1]); return 1`
	value, err := s.client.Eval(ctx, script, []string{key}, rawExpected, userID, source, connectorID).Int64()
	if err != nil {
		return false, err
	}
	switch value {
	case -1:
		return false, errConnectorOAuthForbidden
	case -2:
		return false, errConnectorOAuthProtocol
	default:
		return value == 1, nil
	}
}

// ConnectorOAuthConfig keeps callback destinations and popup origin under server
// control. HTTPClient permits a controlled transport; timeout and redirects are
// enforced independently of the injected client.
type ConnectorOAuthConfig struct {
	GoogleDriveRedirectURI string
	GmailRedirectURI       string
	PopupOrigin            string
	HTTPClient             *http.Client
	AuthorizeConnector     func(context.Context, string, string, string) error
}

type ConnectorOAuthService struct {
	store  ConnectorOAuthStore
	config ConnectorOAuthConfig
	client *http.Client
}

func NewConnectorOAuthService(store ConnectorOAuthStore, config ConnectorOAuthConfig) *ConnectorOAuthService {
	if config.GoogleDriveRedirectURI == "" {
		config.GoogleDriveRedirectURI = os.Getenv("GOOGLE_DRIVE_WEB_OAUTH_REDIRECT_URI")
		if config.GoogleDriveRedirectURI == "" {
			config.GoogleDriveRedirectURI = "http://localhost:9380/api/v1/connectors/google-drive/oauth/web/callback"
		}
	}
	if config.GmailRedirectURI == "" {
		config.GmailRedirectURI = os.Getenv("GMAIL_WEB_OAUTH_REDIRECT_URI")
		if config.GmailRedirectURI == "" {
			config.GmailRedirectURI = "http://localhost:9380/api/v1/connectors/gmail/oauth/web/callback"
		}
	}
	if config.PopupOrigin == "" {
		config.PopupOrigin = os.Getenv("MULTIRAG_CONNECTOR_OAUTH_ORIGIN")
	}
	client := &http.Client{}
	if config.HTTPClient != nil {
		copy := *config.HTTPClient
		client = &copy
	}
	client.Timeout = connectorOAuthTimeout
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	return &ConnectorOAuthService{store: store, config: config, client: client}
}

// SetConnectorOAuthService configures dependencies before the service is used.
func (s *ConnectorService) SetConnectorOAuthService(oauth *ConnectorOAuthService) {
	if oauth != nil && oauth.config.AuthorizeConnector == nil {
		oauth.config.AuthorizeConnector = s.authorizeConnectorOAuth
	}
	s.oauth = oauth
}

func (s *ConnectorService) authorizeConnectorOAuth(ctx context.Context, userID, connectorID, source string) error {
	detail, code, err := s.GetConnector(ctx, connectorID, userID)
	if err != nil {
		switch code {
		case common.CodeAuthenticationError, common.CodePermissionError, common.CodeUnauthorized, common.CodeForbidden:
			return errConnectorOAuthForbidden
		default:
			return errConnectorOAuthUnavailable
		}
	}
	expected := source
	if source == "google-drive" {
		expected = "google_drive"
	}
	if detail.Source != expected {
		return errConnectorOAuthForbidden
	}
	return nil
}

func (s *ConnectorService) connectorOAuth() *ConnectorOAuthService {
	if s.oauth != nil {
		return s.oauth
	}
	var store ConnectorOAuthStore
	if client := cache.Get(); client != nil {
		store = NewConnectorOAuthRedisStore(client.GetClient())
	}
	return NewConnectorOAuthService(store, ConnectorOAuthConfig{AuthorizeConnector: s.authorizeConnectorOAuth})
}

func (s *ConnectorService) StartGoogleWebOAuth(ctx context.Context, userID, source string, req *StartGoogleWebOAuthRequest) (*StartGoogleWebOAuthResponse, common.ErrorCode, error) {
	return s.connectorOAuth().StartGoogleWebOAuth(ctx, userID, source, req)
}

func (s *ConnectorService) PollGoogleWebOAuthResult(ctx context.Context, userID, source string, req *PollGoogleWebOAuthResultRequest) (*PollGoogleWebOAuthResultResponse, common.ErrorCode, error) {
	return s.connectorOAuth().PollGoogleWebOAuthResult(ctx, userID, source, req)
}

func (s *ConnectorService) GoogleWebOAuthCallback(ctx context.Context, source, state, oauthError, code string) string {
	return s.connectorOAuth().GoogleWebOAuthCallback(ctx, source, state, oauthError, code)
}

type connectorOAuthClient struct {
	ClientID     string `json:"client_id"`
	ClientSecret string `json:"client_secret"`
}

type connectorOAuthAttempt struct {
	UserID      string               `json:"user_id"`
	Source      string               `json:"source"`
	ConnectorID string               `json:"connector_id"`
	Client      connectorOAuthClient `json:"client"`
	RedirectURI string               `json:"redirect_uri"`
	PopupOrigin string               `json:"popup_origin"`
	Verifier    string               `json:"verifier"`
}

type connectorOAuthResult struct {
	UserID      string `json:"user_id"`
	Source      string `json:"source"`
	ConnectorID string `json:"connector_id"`
	Credentials string `json:"credentials"`
}

func ConnectorOAuthStateKey(source, flowID string) string {
	return "connector:google:state:" + source + ":" + flowID
}
func ConnectorOAuthResultKey(source, flowID string) string {
	return "connector:google:result:" + source + ":" + flowID
}

func connectorOAuthSource(source string) bool { return source == "google-drive" || source == "gmail" }

func connectorOAuthRandom() (string, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", errConnectorOAuthUnavailable
	}
	return hex.EncodeToString(raw), nil
}

func connectorOAuthFlowID(value string) bool {
	if len(value) != 64 {
		return false
	}
	decoded, err := hex.DecodeString(value)
	return err == nil && len(decoded) == 32 && strings.ToLower(value) == value
}

func connectorOAuthSafeURL(raw string) (*url.URL, error) {
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" || u.User != nil || u.Fragment != "" || u.Opaque != "" {
		return nil, errConnectorOAuthProtocol
	}
	if u.Scheme != "https" {
		ip := net.ParseIP(u.Hostname())
		if u.Scheme != "http" || (u.Hostname() != "localhost" && (ip == nil || !ip.IsLoopback())) {
			return nil, errConnectorOAuthProtocol
		}
	}
	return u, nil
}

func (s *ConnectorOAuthService) destination(source string) (string, string, error) {
	redirect := s.config.GoogleDriveRedirectURI
	if source == "gmail" {
		redirect = s.config.GmailRedirectURI
	}
	u, err := connectorOAuthSafeURL(redirect)
	if err != nil || u.Path != "/api/v1/connectors/"+source+"/oauth/web/callback" || u.RawQuery != "" {
		return "", "", errConnectorOAuthUnavailable
	}
	origin := s.config.PopupOrigin
	if origin == "" {
		origin = u.Scheme + "://" + u.Host
	}
	popup, err := connectorOAuthSafeURL(origin)
	if err != nil || (popup.Path != "" && popup.Path != "/") || popup.RawQuery != "" {
		return "", "", errConnectorOAuthUnavailable
	}
	return redirect, popup.Scheme + "://" + popup.Host, nil
}

func connectorOAuthString(data map[string]json.RawMessage, key string, required bool) (string, error) {
	raw, ok := data[key]
	if !ok && !required {
		return "", nil
	}
	var value string
	if !ok || bytes.Equal(bytes.TrimSpace(raw), []byte("null")) || json.Unmarshal(raw, &value) != nil || (required && strings.TrimSpace(value) == "") {
		return "", errConnectorOAuthProtocol
	}
	return value, nil
}

func connectorGoogleClient(raw json.RawMessage, redirect string) (connectorOAuthClient, error) {
	if len(raw) == 0 || len(raw) > 128<<10 {
		return connectorOAuthClient{}, errConnectorOAuthProtocol
	}
	if bytes.HasPrefix(bytes.TrimSpace(raw), []byte(`"`)) {
		var text string
		if json.Unmarshal(raw, &text) != nil {
			return connectorOAuthClient{}, errConnectorOAuthProtocol
		}
		raw = []byte(text)
	}
	data, err := strictJSONObject(raw)
	if err != nil {
		return connectorOAuthClient{}, err
	}
	if refresh, ok := data["refresh_token"]; ok && string(refresh) != `""` && string(refresh) != "null" {
		return connectorOAuthClient{}, errConnectorOAuthProtocol
	}
	web, err := strictJSONObject(data["web"])
	if err != nil {
		return connectorOAuthClient{}, err
	}
	id, err := connectorOAuthString(web, "client_id", true)
	if err != nil || len(id) > 1024 {
		return connectorOAuthClient{}, errConnectorOAuthProtocol
	}
	secret, err := connectorOAuthString(web, "client_secret", true)
	if err != nil || len(secret) > 4096 {
		return connectorOAuthClient{}, errConnectorOAuthProtocol
	}
	for key, allowed := range map[string][]string{"auth_uri": {connectorGoogleAuthURL, "https://accounts.google.com/o/oauth2/auth"}, "token_uri": {connectorGoogleTokenURL}} {
		value, err := connectorOAuthString(web, key, false)
		if err != nil || (value != "" && !slices.Contains(allowed, value)) {
			return connectorOAuthClient{}, errConnectorOAuthProtocol
		}
	}
	if raw, ok := web["redirect_uris"]; ok {
		var redirects []string
		if string(raw) == "null" || json.Unmarshal(raw, &redirects) != nil || !slices.Contains(redirects, redirect) {
			return connectorOAuthClient{}, errConnectorOAuthProtocol
		}
	}
	return connectorOAuthClient{ClientID: id, ClientSecret: secret}, nil
}

func connectorGoogleScopes(source string) []string {
	scopes := []string{"https://www.googleapis.com/auth/admin.directory.user.readonly", "https://www.googleapis.com/auth/admin.directory.group.readonly"}
	if source == "gmail" {
		return append([]string{"https://www.googleapis.com/auth/gmail.readonly"}, scopes...)
	}
	return append([]string{"https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/drive.metadata.readonly"}, scopes...)
}

func (s *ConnectorOAuthService) connectorAccess(ctx context.Context, userID, connectorID, source string) error {
	if ctx.Err() != nil {
		return errConnectorOAuthUnavailable
	}
	if connectorID == "" {
		return nil
	}
	if s.config.AuthorizeConnector == nil {
		return errConnectorOAuthForbidden
	}
	if err := s.config.AuthorizeConnector(ctx, userID, connectorID, source); err != nil {
		if errors.Is(err, errConnectorOAuthForbidden) {
			return errConnectorOAuthForbidden
		}
		return errConnectorOAuthUnavailable
	}
	if ctx.Err() != nil {
		return errConnectorOAuthUnavailable
	}
	return nil
}

func (s *ConnectorOAuthService) StartGoogleWebOAuth(ctx context.Context, userID, source string, req *StartGoogleWebOAuthRequest) (*StartGoogleWebOAuthResponse, common.ErrorCode, error) {
	if userID == "" {
		return nil, common.CodeAuthenticationError, errConnectorOAuthForbidden
	}
	if !connectorOAuthSource(source) || req == nil {
		return nil, common.CodeArgumentError, fmt.Errorf("invalid Google authorization request")
	}
	redirect, origin, err := s.destination(source)
	if err != nil {
		return nil, common.CodeServerError, err
	}
	if req.RedirectURI != "" && req.RedirectURI != redirect {
		return nil, common.CodeArgumentError, fmt.Errorf("redirect_uri must match the server callback")
	}
	client, err := connectorGoogleClient(req.Credentials, redirect)
	if err != nil {
		return nil, common.CodeArgumentError, fmt.Errorf("invalid Google web client credentials")
	}
	if err = s.connectorAccess(ctx, userID, req.ConnectorID, source); err != nil {
		if errors.Is(err, errConnectorOAuthForbidden) {
			return nil, common.CodePermissionError, errConnectorOAuthForbidden
		}
		return nil, common.CodeServerError, errConnectorOAuthUnavailable
	}
	if s.store == nil || ctx.Err() != nil {
		return nil, common.CodeServerError, errConnectorOAuthUnavailable
	}
	flowID, err := connectorOAuthRandom()
	if err != nil {
		return nil, common.CodeServerError, err
	}
	verifier, err := connectorOAuthRandom()
	if err != nil {
		return nil, common.CodeServerError, err
	}
	attempt := connectorOAuthAttempt{UserID: userID, Source: source, ConnectorID: req.ConnectorID, Client: client, RedirectURI: redirect, PopupOrigin: origin, Verifier: verifier}
	raw, err := json.Marshal(attempt)
	if err != nil {
		return nil, common.CodeServerError, errConnectorOAuthUnavailable
	}
	if ok, err := s.store.SetNX(ctx, ConnectorOAuthStateKey(source, flowID), string(raw), connectorOAuthTTL); err != nil || !ok {
		return nil, common.CodeServerError, errConnectorOAuthUnavailable
	}
	digest := sha256.Sum256([]byte(verifier))
	query := url.Values{"client_id": {client.ClientID}, "redirect_uri": {redirect}, "response_type": {"code"}, "scope": {strings.Join(connectorGoogleScopes(source), " ")}, "access_type": {"offline"}, "include_granted_scopes": {"true"}, "prompt": {"consent"}, "state": {flowID}, "code_challenge": {base64.RawURLEncoding.EncodeToString(digest[:])}, "code_challenge_method": {"S256"}}
	return &StartGoogleWebOAuthResponse{FlowID: flowID, AuthorizationURL: connectorGoogleAuthURL + "?" + query.Encode(), ExpiresIn: int64(connectorOAuthTTL.Seconds())}, common.CodeSuccess, nil
}

func (s *ConnectorOAuthService) exchange(ctx context.Context, attempt connectorOAuthAttempt, code string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, connectorOAuthTimeout)
	defer cancel()
	form := url.Values{"client_id": {attempt.Client.ClientID}, "client_secret": {attempt.Client.ClientSecret}, "code": {code}, "redirect_uri": {attempt.RedirectURI}, "grant_type": {"authorization_code"}, "code_verifier": {attempt.Verifier}}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, connectorGoogleTokenURL, strings.NewReader(form.Encode()))
	if err != nil {
		return "", errConnectorOAuthProtocol
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")
	response, err := s.client.Do(req)
	if err != nil {
		return "", errConnectorOAuthProtocol
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return "", errConnectorOAuthProtocol
	}
	raw, err := io.ReadAll(io.LimitReader(response.Body, connectorOAuthLimit+1))
	if err != nil || len(raw) > connectorOAuthLimit {
		return "", errConnectorOAuthProtocol
	}
	data, err := strictJSONObject(raw)
	if err != nil {
		return "", errConnectorOAuthProtocol
	}
	if _, exists := data["error"]; exists {
		return "", errConnectorOAuthProtocol
	}
	token, err := connectorOAuthString(data, "access_token", true)
	if err != nil || strings.ContainsAny(token, "\r\n") {
		return "", errConnectorOAuthProtocol
	}
	tokenType, err := connectorOAuthString(data, "token_type", true)
	if err != nil || !strings.EqualFold(tokenType, "bearer") {
		return "", errConnectorOAuthProtocol
	}
	refresh, err := connectorOAuthString(data, "refresh_token", false)
	if err != nil || strings.ContainsAny(refresh, "\r\n") {
		return "", errConnectorOAuthProtocol
	}
	scope, err := connectorOAuthString(data, "scope", false)
	if err != nil {
		return "", errConnectorOAuthProtocol
	}
	scopes := connectorGoogleScopes(attempt.Source)
	if scope != "" {
		scopes = strings.Fields(scope)
	}
	credentials := map[string]any{"token": token, "token_uri": connectorGoogleTokenURL, "client_id": attempt.Client.ClientID, "client_secret": attempt.Client.ClientSecret, "scopes": scopes}
	if refresh != "" {
		credentials["refresh_token"] = refresh
	}
	if raw, exists := data["expires_in"]; exists {
		var seconds int64
		if string(raw) == "null" || json.Unmarshal(raw, &seconds) != nil || seconds < 0 || seconds > int64((1<<63-1)/int64(time.Second)) {
			return "", errConnectorOAuthProtocol
		}
		credentials["expiry"] = time.Now().Add(time.Duration(seconds) * time.Second).UTC().Format(time.RFC3339Nano)
	}
	result, err := json.Marshal(credentials)
	if err != nil {
		return "", errConnectorOAuthProtocol
	}
	return string(result), nil
}

func (s *ConnectorOAuthService) GoogleWebOAuthCallback(ctx context.Context, source, state, oauthError, code string) string {
	_, origin, _ := s.destination(source)
	failure := func() string { return connectorOAuthPopup(state, source, origin, false) }
	if !connectorOAuthSource(source) || !connectorOAuthFlowID(state) || s.store == nil {
		return failure()
	}
	raw, err := s.store.Take(ctx, ConnectorOAuthStateKey(source, state))
	if err != nil {
		return failure()
	}
	var attempt connectorOAuthAttempt
	if json.Unmarshal([]byte(raw), &attempt) != nil || attempt.UserID == "" || attempt.Source != source || !connectorOAuthFlowID(attempt.Verifier) || attempt.Client.ClientID == "" || attempt.Client.ClientSecret == "" {
		return failure()
	}
	redirect, expectedOrigin, err := s.destination(source)
	if err != nil || attempt.RedirectURI != redirect || attempt.PopupOrigin != expectedOrigin {
		return failure()
	}
	origin = attempt.PopupOrigin
	if oauthError != "" || strings.TrimSpace(code) == "" || len(code) > 16<<10 || s.connectorAccess(ctx, attempt.UserID, attempt.ConnectorID, source) != nil {
		return failure()
	}
	credentials, err := s.exchange(ctx, attempt, code)
	if err != nil {
		return failure()
	}
	result := connectorOAuthResult{UserID: attempt.UserID, Source: source, ConnectorID: attempt.ConnectorID, Credentials: credentials}
	encoded, err := json.Marshal(result)
	if err != nil {
		return failure()
	}
	if ok, err := s.store.SetNX(ctx, ConnectorOAuthResultKey(source, state), string(encoded), connectorOAuthTTL); err != nil || !ok {
		return failure()
	}
	return connectorOAuthPopup(state, source, origin, true)
}

func (s *ConnectorOAuthService) PollGoogleWebOAuthResult(ctx context.Context, userID, source string, req *PollGoogleWebOAuthResultRequest) (*PollGoogleWebOAuthResultResponse, common.ErrorCode, error) {
	if userID == "" {
		return nil, common.CodeAuthenticationError, errConnectorOAuthForbidden
	}
	if !connectorOAuthSource(source) || req == nil || !connectorOAuthFlowID(req.FlowID) {
		return nil, common.CodeArgumentError, fmt.Errorf("invalid authorization flow")
	}
	if s.store == nil {
		return nil, common.CodeServerError, errConnectorOAuthUnavailable
	}
	key := ConnectorOAuthResultKey(source, req.FlowID)
	raw, err := s.store.PeekOwned(ctx, key, userID, source, req.ConnectorID)
	if errors.Is(err, errConnectorOAuthMissing) {
		return nil, common.CodeRunning, errConnectorOAuthMissing
	}
	if errors.Is(err, errConnectorOAuthForbidden) {
		return nil, common.CodePermissionError, errConnectorOAuthForbidden
	}
	if err != nil {
		return nil, common.CodeServerError, errConnectorOAuthUnavailable
	}
	var result connectorOAuthResult
	if json.Unmarshal([]byte(raw), &result) != nil || result.UserID != userID || result.Source != source || result.Credentials == "" || (req.ConnectorID != "" && req.ConnectorID != result.ConnectorID) {
		return nil, common.CodeServerError, errConnectorOAuthUnavailable
	}
	if err := s.connectorAccess(ctx, userID, result.ConnectorID, source); err != nil {
		if errors.Is(err, errConnectorOAuthForbidden) {
			return nil, common.CodePermissionError, errConnectorOAuthForbidden
		}
		return nil, common.CodeServerError, errConnectorOAuthUnavailable
	}
	consumed, err := s.store.ConsumeOwned(ctx, key, raw, userID, source, req.ConnectorID)
	if errors.Is(err, errConnectorOAuthForbidden) {
		return nil, common.CodePermissionError, errConnectorOAuthForbidden
	}
	if err != nil {
		return nil, common.CodeServerError, errConnectorOAuthUnavailable
	}
	if !consumed {
		return nil, common.CodeRunning, errConnectorOAuthMissing
	}
	return &PollGoogleWebOAuthResultResponse{Credentials: result.Credentials}, common.CodeSuccess, nil
}

func connectorOAuthPopup(flowID, source, origin string, success bool) string {
	status, heading := "error", "Authorization failed. Please restart from the main window."
	closeScript := ""
	if success {
		status, heading, closeScript = "success", "Authorization completed successfully.", "window.close();"
	}
	if !connectorOAuthFlowID(flowID) {
		flowID = ""
	}
	payload, _ := json.Marshal(map[string]string{"type": "multirag-" + source + "-oauth", "status": status, "flowId": flowID, "message": heading})
	target, _ := json.Marshal(origin)
	script := ""
	if connectorOAuthSource(source) && origin != "" {
		script = "if(window.opener){window.opener.postMessage(" + string(payload) + "," + string(target) + ");}" + closeScript
	}
	return `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>Google authorization</title></head><body><h1>` + html.EscapeString(heading) + `</h1><p>You can close this window.</p><script>` + script + `</script></body></html>`
}
