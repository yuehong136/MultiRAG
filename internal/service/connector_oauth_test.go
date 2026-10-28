package service

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"multirag/internal/common"
	"multirag/internal/testsupport"
)

type connectorOAuthMemoryStore struct {
	mu                             sync.Mutex
	values                         map[string]string
	journal                        *testsupport.CaseJournal
	failSet, failTake, failConsume bool
}

func (s *connectorOAuthMemoryStore) SetNX(ctx context.Context, key, raw string, ttl time.Duration) (bool, error) {
	if ctx.Err() != nil {
		return false, ctx.Err()
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if ttl != 15*time.Minute {
		return false, errors.New("wrong expiry")
	}
	if s.failSet {
		return false, errors.New("private Redis failure")
	}
	if _, exists := s.values[key]; exists {
		return false, nil
	}
	s.journal.Event("reserve-memory-oauth-key", map[string]any{"key": key})
	s.values[key] = raw
	return true, nil
}

func (s *connectorOAuthMemoryStore) Take(ctx context.Context, key string) (string, error) {
	if ctx.Err() != nil {
		return "", ctx.Err()
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.failTake {
		return "", errors.New("private Redis failure")
	}
	raw, ok := s.values[key]
	if !ok {
		return "", errConnectorOAuthMissing
	}
	delete(s.values, key)
	return raw, nil
}

func (s *connectorOAuthMemoryStore) PeekOwned(ctx context.Context, key, userID, source, connectorID string) (string, error) {
	if ctx.Err() != nil {
		return "", ctx.Err()
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.failTake {
		return "", errors.New("private Redis failure")
	}
	raw, exists := s.values[key]
	if !exists {
		return "", errConnectorOAuthMissing
	}
	var result connectorOAuthResult
	if json.Unmarshal([]byte(raw), &result) != nil {
		return "", errConnectorOAuthProtocol
	}
	if result.UserID != userID || result.Source != source || (connectorID != "" && result.ConnectorID != connectorID) {
		return "", errConnectorOAuthForbidden
	}
	return raw, nil
}

func (s *connectorOAuthMemoryStore) ConsumeOwned(ctx context.Context, key, rawExpected, userID, source, connectorID string) (bool, error) {
	if ctx.Err() != nil {
		return false, ctx.Err()
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.failConsume {
		return false, errors.New("private Redis failure")
	}
	raw, exists := s.values[key]
	if !exists {
		return false, nil
	}
	var result connectorOAuthResult
	if json.Unmarshal([]byte(raw), &result) != nil {
		return false, errConnectorOAuthProtocol
	}
	if result.UserID != userID || result.Source != source || (connectorID != "" && result.ConnectorID != connectorID) {
		return false, errConnectorOAuthForbidden
	}
	if raw != rawExpected {
		return false, nil
	}
	delete(s.values, key)
	return true, nil
}

func (s *connectorOAuthMemoryStore) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.values)
}

type connectorOAuthTransport func(*http.Request) (*http.Response, error)

func (f connectorOAuthTransport) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }

func connectorOAuthResponse(status int, body string) *http.Response {
	return &http.Response{StatusCode: status, Header: http.Header{}, Body: io.NopCloser(strings.NewReader(body))}
}

func newConnectorOAuthTest(t *testing.T, transport connectorOAuthTransport) (*ConnectorOAuthService, *connectorOAuthMemoryStore) {
	t.Helper()
	store := &connectorOAuthMemoryStore{values: map[string]string{}, journal: testsupport.NewCase(t)}
	s := NewConnectorOAuthService(store, ConnectorOAuthConfig{
		GoogleDriveRedirectURI: "https://app.invalid/api/v1/connectors/google-drive/oauth/web/callback",
		GmailRedirectURI:       "https://app.invalid/api/v1/connectors/gmail/oauth/web/callback",
		PopupOrigin:            "https://frontend.invalid",
		HTTPClient:             &http.Client{Transport: transport},
		AuthorizeConnector: func(_ context.Context, userID, connectorID, source string) error {
			if userID != "owner" || connectorID != "connector" || !connectorOAuthSource(source) {
				return errConnectorOAuthForbidden
			}
			return nil
		},
	})
	return s, store
}

func connectorOAuthCredentials(t *testing.T, source string, asString bool) json.RawMessage {
	t.Helper()
	raw := `{"web":{"client_id":"owned-client","client_secret":"owned-client-secret","auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token","redirect_uris":["https://app.invalid/api/v1/connectors/` + source + `/oauth/web/callback"]}}`
	if asString {
		encoded, err := json.Marshal(raw)
		if err != nil {
			t.Fatal(err)
		}
		return encoded
	}
	return []byte(raw)
}

func startConnectorOAuthTest(t *testing.T, s *ConnectorOAuthService, source string) *StartGoogleWebOAuthResponse {
	t.Helper()
	response, code, err := s.StartGoogleWebOAuth(context.Background(), "owner", source, &StartGoogleWebOAuthRequest{Credentials: connectorOAuthCredentials(t, source, true), ConnectorID: "connector"})
	if err != nil || code != common.CodeSuccess {
		t.Fatalf("start failed: code=%d err=%v", code, err)
	}
	return response
}

func TestConnectorOAuthPKCEOwnerSourceAndSingleConsumption(t *testing.T) {
	for _, source := range []string{"google-drive", "gmail"} {
		t.Run(source, func(t *testing.T) {
			var expectedChallenge string
			var calls atomic.Int64
			s, store := newConnectorOAuthTest(t, func(req *http.Request) (*http.Response, error) {
				calls.Add(1)
				if req.URL.String() != connectorGoogleTokenURL || req.Method != http.MethodPost {
					t.Error("token request escaped fixed endpoint")
				}
				if err := req.ParseForm(); err != nil {
					t.Error(err)
				}
				form := req.PostForm
				digest := sha256.Sum256([]byte(form.Get("code_verifier")))
				if base64.RawURLEncoding.EncodeToString(digest[:]) != expectedChallenge || form.Get("client_secret") != "owned-client-secret" || form.Get("code") != "owned-code" || form.Get("redirect_uri") != "https://app.invalid/api/v1/connectors/"+source+"/oauth/web/callback" || form.Get("grant_type") != "authorization_code" {
					t.Error("token request lost flow binding")
				}
				deadline, ok := req.Context().Deadline()
				if !ok || time.Until(deadline) > 7*time.Second {
					t.Error("unbounded token request")
				}
				return connectorOAuthResponse(200, `{"access_token":"owned-access","refresh_token":"owned-refresh","token_type":"Bearer","expires_in":0}`), nil
			})
			flow := startConnectorOAuthTest(t, s, source)
			auth, err := url.Parse(flow.AuthorizationURL)
			if err != nil {
				t.Fatal(err)
			}
			query := auth.Query()
			expectedChallenge = query.Get("code_challenge")
			if flow.ExpiresIn != 900 || !connectorOAuthFlowID(flow.FlowID) || auth.Scheme != "https" || auth.Host != "accounts.google.com" || query.Get("state") != flow.FlowID || query.Get("code_challenge_method") != "S256" || expectedChallenge == "" || query.Get("code_verifier") != "" || strings.Contains(flow.AuthorizationURL, "owned-client-secret") {
				t.Fatal("unsafe authorization response")
			}
			wrongSource := "gmail"
			if source == "gmail" {
				wrongSource = "google-drive"
			}
			if html := s.GoogleWebOAuthCallback(context.Background(), wrongSource, flow.FlowID, "", "owned-code"); strings.Contains(html, `"status":"success"`) || calls.Load() != 0 || store.count() != 1 {
				t.Fatal("wrong source consumed state or made request")
			}
			html := s.GoogleWebOAuthCallback(context.Background(), source, flow.FlowID, "", "owned-code")
			if !strings.Contains(html, `"status":"success"`) || !strings.Contains(html, `"multirag-`+source+`-oauth"`) || !strings.Contains(html, `,"https://frontend.invalid")`) || strings.Contains(html, `"*"`) {
				t.Fatal("popup has incorrect status or origin")
			}
			for _, secret := range []string{"owned-access", "owned-refresh", "owned-client-secret", "owned-code"} {
				if strings.Contains(html, secret) {
					t.Fatal("popup leaked credentials")
				}
			}
			if _, code, err := s.PollGoogleWebOAuthResult(context.Background(), "foreign", source, &PollGoogleWebOAuthResultRequest{FlowID: flow.FlowID}); err == nil || code != common.CodePermissionError || store.count() != 1 {
				t.Fatal("cross-user poll consumed owner result")
			}
			if _, code, err := s.PollGoogleWebOAuthResult(context.Background(), "owner", wrongSource, &PollGoogleWebOAuthResultRequest{FlowID: flow.FlowID}); err == nil || code != common.CodeRunning || store.count() != 1 {
				t.Fatal("cross-source poll consumed owner result")
			}
			if _, code, err := s.PollGoogleWebOAuthResult(context.Background(), "owner", source, &PollGoogleWebOAuthResultRequest{FlowID: flow.FlowID, ConnectorID: "foreign"}); err == nil || code != common.CodePermissionError || store.count() != 1 {
				t.Fatal("cross-connector poll consumed owner result")
			}
			result, code, err := s.PollGoogleWebOAuthResult(context.Background(), "owner", source, &PollGoogleWebOAuthResultRequest{FlowID: flow.FlowID, ConnectorID: "connector"})
			if err != nil || code != common.CodeSuccess || store.count() != 0 {
				t.Fatal("owner could not consume result", err)
			}
			var credentials map[string]any
			if json.Unmarshal([]byte(result.Credentials), &credentials) != nil || credentials["token"] != "owned-access" || credentials["refresh_token"] != "owned-refresh" || credentials["token_uri"] != connectorGoogleTokenURL || credentials["client_secret"] != "owned-client-secret" || credentials["expiry"] == "" {
				t.Fatal("credential serialization or legal zero expiry lost")
			}
			if _, code, err := s.PollGoogleWebOAuthResult(context.Background(), "owner", source, &PollGoogleWebOAuthResultRequest{FlowID: flow.FlowID}); err == nil || code != common.CodeRunning {
				t.Fatal("result replay succeeded")
			}
			if html := s.GoogleWebOAuthCallback(context.Background(), source, flow.FlowID, "", "owned-code"); strings.Contains(html, `"status":"success"`) || calls.Load() != 1 {
				t.Fatal("callback replay made provider request")
			}
		})
	}
}

func TestConnectorOAuthConcurrentCallbackAndPoll(t *testing.T) {
	var calls atomic.Int64
	s, store := newConnectorOAuthTest(t, func(*http.Request) (*http.Response, error) {
		calls.Add(1)
		return connectorOAuthResponse(200, `{"access_token":"owned-access","token_type":"Bearer"}`), nil
	})
	flow := startConnectorOAuthTest(t, s, "google-drive")
	var workers sync.WaitGroup
	var successes atomic.Int64
	for range 8 {
		workers.Add(1)
		go func() {
			defer workers.Done()
			if strings.Contains(s.GoogleWebOAuthCallback(context.Background(), "google-drive", flow.FlowID, "", "code"), `"status":"success"`) {
				successes.Add(1)
			}
		}()
	}
	workers.Wait()
	if successes.Load() != 1 || calls.Load() != 1 {
		t.Fatal("callback state was not atomically consumed")
	}
	successes.Store(0)
	for range 8 {
		workers.Add(1)
		go func() {
			defer workers.Done()
			result, code, err := s.PollGoogleWebOAuthResult(context.Background(), "owner", "google-drive", &PollGoogleWebOAuthResultRequest{FlowID: flow.FlowID})
			if err == nil && code == common.CodeSuccess && result.Credentials != "" {
				successes.Add(1)
			}
		}()
	}
	workers.Wait()
	if successes.Load() != 1 || store.count() != 0 {
		t.Fatal("result was not atomically consumed")
	}
}

func TestConnectorOAuthPollFailuresPreserveRetryableResult(t *testing.T) {
	for _, mode := range []string{"sql-error", "cancel-before-peek", "cancel-during-sql", "permission-denied", "peek-error", "consume-error"} {
		t.Run(mode, func(t *testing.T) {
			s, store := newConnectorOAuthTest(t, func(*http.Request) (*http.Response, error) {
				return connectorOAuthResponse(200, `{"access_token":"owned-access","token_type":"Bearer"}`), nil
			})
			flow := startConnectorOAuthTest(t, s, "google-drive")
			if html := s.GoogleWebOAuthCallback(context.Background(), "google-drive", flow.FlowID, "", "code"); !strings.Contains(html, `"status":"success"`) {
				t.Fatal("fixture authorization failed")
			}
			key := ConnectorOAuthResultKey("google-drive", flow.FlowID)
			store.mu.Lock()
			before := store.values[key]
			store.mu.Unlock()
			originalAuthorize := s.config.AuthorizeConnector
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			switch mode {
			case "sql-error":
				s.config.AuthorizeConnector = func(context.Context, string, string, string) error { return errors.New("private-SQL-secret") }
			case "cancel-before-peek":
				cancel()
			case "cancel-during-sql":
				s.config.AuthorizeConnector = func(context.Context, string, string, string) error { cancel(); return nil }
			case "permission-denied":
				s.config.AuthorizeConnector = func(context.Context, string, string, string) error { return errConnectorOAuthForbidden }
			case "peek-error":
				store.failTake = true
			case "consume-error":
				store.failConsume = true
			}
			result, code, err := s.PollGoogleWebOAuthResult(ctx, "owner", "google-drive", &PollGoogleWebOAuthResultRequest{FlowID: flow.FlowID})
			expected := common.CodeServerError
			if mode == "permission-denied" {
				expected = common.CodePermissionError
			}
			if result != nil || code != expected || err == nil || strings.Contains(err.Error(), "private-") {
				t.Fatal("poll failure returned credentials or lost error classification")
			}
			store.mu.Lock()
			after := store.values[key]
			store.mu.Unlock()
			if before == "" || after != before {
				t.Fatal("retryable poll failure consumed or changed result")
			}
			s.config.AuthorizeConnector = originalAuthorize
			store.failTake, store.failConsume = false, false
			result, code, err = s.PollGoogleWebOAuthResult(context.Background(), "owner", "google-drive", &PollGoogleWebOAuthResultRequest{FlowID: flow.FlowID})
			if err != nil || code != common.CodeSuccess || !strings.Contains(result.Credentials, "owned-access") || store.count() != 0 {
				t.Fatal("owner could not retry preserved result")
			}
		})
	}
}

func TestConnectorOAuthPollCASRejectsReplacedAndExpiredResult(t *testing.T) {
	for _, mode := range []string{"replaced", "expired"} {
		t.Run(mode, func(t *testing.T) {
			s, store := newConnectorOAuthTest(t, func(*http.Request) (*http.Response, error) {
				return connectorOAuthResponse(200, `{"access_token":"owned-access","token_type":"Bearer"}`), nil
			})
			flow := startConnectorOAuthTest(t, s, "google-drive")
			if html := s.GoogleWebOAuthCallback(context.Background(), "google-drive", flow.FlowID, "", "code"); !strings.Contains(html, `"status":"success"`) {
				t.Fatal("fixture authorization failed")
			}
			key := ConnectorOAuthResultKey("google-drive", flow.FlowID)
			originalAuthorize := s.config.AuthorizeConnector
			s.config.AuthorizeConnector = func(context.Context, string, string, string) error {
				store.journal.Event("reserve-memory-result-race", map[string]any{"key": key, "mode": mode})
				store.mu.Lock()
				defer store.mu.Unlock()
				if mode == "expired" {
					delete(store.values, key)
				} else {
					store.values[key] += " "
				}
				return nil
			}
			if result, code, err := s.PollGoogleWebOAuthResult(context.Background(), "owner", "google-drive", &PollGoogleWebOAuthResultRequest{FlowID: flow.FlowID}); result != nil || code != common.CodeRunning || err == nil {
				t.Fatal("CAS returned stale or already consumed credentials")
			}
			if mode == "replaced" {
				if store.count() != 1 {
					t.Fatal("failed CAS deleted replacement")
				}
				s.config.AuthorizeConnector = originalAuthorize
				if result, code, err := s.PollGoogleWebOAuthResult(context.Background(), "owner", "google-drive", &PollGoogleWebOAuthResultRequest{FlowID: flow.FlowID}); err != nil || code != common.CodeSuccess || result.Credentials == "" {
					t.Fatal("fresh CAS could not consume replacement")
				}
			} else if store.count() != 0 {
				t.Fatal("CAS resurrected expired result")
			}
		})
	}
}

func TestConnectorOAuthStartDistinguishesSQLFailureFromPermissionDenial(t *testing.T) {
	s, store := newConnectorOAuthTest(t, nil)
	s.config.AuthorizeConnector = func(context.Context, string, string, string) error { return errors.New("private-SQL-secret") }
	if result, code, err := s.StartGoogleWebOAuth(context.Background(), "owner", "google-drive", &StartGoogleWebOAuthRequest{Credentials: connectorOAuthCredentials(t, "google-drive", false), ConnectorID: "connector"}); result != nil || code != common.CodeServerError || err == nil || strings.Contains(err.Error(), "private-") || store.count() != 0 {
		t.Fatal("transient SQL failure became permission denial or wrote state")
	}
}

func TestConnectorOAuthRejectsUntrustedConfigurationWithoutWrites(t *testing.T) {
	for _, raw := range []string{
		`null`, `[]`, `"invalid"`, `{}`, `{"web":null}`,
		`{"web":{"client_id":"id","client_secret":"secret","token_uri":"https://attacker.invalid/token"}}`,
		`{"web":{"client_id":"id","client_secret":"secret","auth_uri":"javascript:alert(1)"}}`,
		`{"web":{"client_id":"id","client_secret":"secret","token_uri":"http://oauth2.googleapis.com/token"}}`,
		`{"web":{"client_id":"id","client_secret":"secret","redirect_uris":[]}}`,
		`{"web":{"client_id":"id","client_secret":"secret","redirect_uris":null}}`,
		`{"web":{"client_id":"id","client_id":"other","client_secret":"secret"}}`,
		`{"web":{"client_id":"id","client_secret":"secret"},"refresh_token":"existing"}`,
		`{"web":{"client_id":0,"client_secret":"secret"}}`,
		`{"web":{"client_id":"id","client_secret":null}}`,
	} {
		t.Run(raw, func(t *testing.T) {
			s, store := newConnectorOAuthTest(t, func(*http.Request) (*http.Response, error) {
				t.Fatal("validation made outbound request")
				return nil, nil
			})
			_, code, err := s.StartGoogleWebOAuth(context.Background(), "owner", "google-drive", &StartGoogleWebOAuthRequest{Credentials: []byte(raw)})
			if err == nil || code != common.CodeArgumentError || store.count() != 0 {
				t.Fatal("untrusted configuration accepted")
			}
		})
	}
	s, store := newConnectorOAuthTest(t, nil)
	request := &StartGoogleWebOAuthRequest{Credentials: connectorOAuthCredentials(t, "google-drive", false), ConnectorID: "foreign"}
	if _, code, err := s.StartGoogleWebOAuth(context.Background(), "owner", "google-drive", request); err == nil || code != common.CodePermissionError || store.count() != 0 {
		t.Fatal("foreign connector acquired OAuth state")
	}
	request.ConnectorID = ""
	request.RedirectURI = "https://attacker.invalid/api/v1/connectors/google-drive/oauth/web/callback"
	if _, code, err := s.StartGoogleWebOAuth(context.Background(), "owner", "google-drive", request); err == nil || code != common.CodeArgumentError || store.count() != 0 {
		t.Fatal("redirect override accepted")
	}
	request.RedirectURI = ""
	for _, origin := range []string{"*", "https://user:secret@frontend.invalid", "http://frontend.invalid", "https://frontend.invalid/path", "https://frontend.invalid#fragment"} {
		s.config.PopupOrigin = origin
		if _, _, err := s.StartGoogleWebOAuth(context.Background(), "owner", "google-drive", request); err == nil || store.count() != 0 {
			t.Fatal("untrusted popup origin accepted")
		}
	}
}

func TestConnectorOAuthTokenFailuresAreBoundedAndSecretSafe(t *testing.T) {
	cases := []struct {
		name, body     string
		status         int
		transportError bool
	}{
		{"provider-error", `{"error":"private-provider-error","error_description":"private-provider-secret"}`, 400, false},
		{"redirect", `private-provider-secret`, 302, false},
		{"malformed", `private-provider-secret`, 200, false},
		{"oversize", strings.Repeat(" ", connectorOAuthLimit+1), 200, false},
		{"empty", `{}`, 200, false},
		{"null", `null`, 200, false},
		{"duplicate", `{"access_token":"a","access_token":"b","token_type":"Bearer"}`, 200, false},
		{"wrong-token-type", `{"access_token":0,"token_type":"Bearer"}`, 200, false},
		{"missing-bearer", `{"access_token":"a"}`, 200, false},
		{"unsupported-token", `{"access_token":"a","token_type":"MAC"}`, 200, false},
		{"newline-token", `{"access_token":"a\r\nprivate-provider-secret","token_type":"Bearer"}`, 200, false},
		{"null-expiry", `{"access_token":"a","token_type":"Bearer","expires_in":null}`, 200, false},
		{"negative-expiry", `{"access_token":"a","token_type":"Bearer","expires_in":-1}`, 200, false},
		{"fraction-expiry", `{"access_token":"a","token_type":"Bearer","expires_in":0.5}`, 200, false},
		{"wrong-refresh", `{"access_token":"a","token_type":"Bearer","refresh_token":false}`, 200, false},
		{"transport-error", ``, 0, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var calls atomic.Int64
			s, store := newConnectorOAuthTest(t, func(*http.Request) (*http.Response, error) {
				calls.Add(1)
				if tc.transportError {
					return nil, errors.New("private-provider-secret")
				}
				response := connectorOAuthResponse(tc.status, tc.body)
				if tc.name == "redirect" {
					response.Header.Set("Location", "https://attacker.invalid/token")
				}
				return response, nil
			})
			flow := startConnectorOAuthTest(t, s, "google-drive")
			html := s.GoogleWebOAuthCallback(context.Background(), "google-drive", flow.FlowID, "", "private-code")
			if strings.Contains(html, `"status":"success"`) || strings.Contains(html, "private-") || store.count() != 0 || calls.Load() != 1 {
				t.Fatal("provider failure succeeded, leaked details or retained credentials")
			}
			if _, code, _ := s.PollGoogleWebOAuthResult(context.Background(), "owner", "google-drive", &PollGoogleWebOAuthResultRequest{FlowID: flow.FlowID}); code != common.CodeRunning {
				t.Fatal("provider failure left usable result")
			}
		})
	}
}

func TestConnectorOAuthCancellationRevocationAndCacheFailure(t *testing.T) {
	for _, mode := range []string{"cancelled", "missing-code", "oauth-error", "revoked", "result-write-failure", "state-read-failure"} {
		t.Run(mode, func(t *testing.T) {
			var calls atomic.Int64
			var s *ConnectorOAuthService
			var store *connectorOAuthMemoryStore
			s, store = newConnectorOAuthTest(t, func(req *http.Request) (*http.Response, error) {
				calls.Add(1)
				if mode == "result-write-failure" {
					store.mu.Lock()
					store.failSet = true
					store.mu.Unlock()
				}
				return connectorOAuthResponse(200, `{"access_token":"owned-access","token_type":"Bearer"}`), nil
			})
			flow := startConnectorOAuthTest(t, s, "google-drive")
			ctx := context.Background()
			code, oauthError := "code", ""
			if mode == "cancelled" {
				cancelled, cancel := context.WithCancel(ctx)
				cancel()
				ctx = cancelled
			}
			if mode == "missing-code" {
				code = ""
			}
			if mode == "oauth-error" {
				oauthError = "private-provider-secret"
			}
			if mode == "revoked" {
				s.config.AuthorizeConnector = func(context.Context, string, string, string) error { return errors.New("private-SQL-error") }
			}
			if mode == "state-read-failure" {
				store.failTake = true
			}
			html := s.GoogleWebOAuthCallback(ctx, "google-drive", flow.FlowID, oauthError, code)
			if strings.Contains(html, `"status":"success"`) || strings.Contains(html, "private-") {
				t.Fatal("failure became success or leaked details")
			}
			expectedCalls := int64(0)
			if mode == "result-write-failure" {
				expectedCalls = 1
			}
			if calls.Load() != expectedCalls {
				t.Fatal("denied callback called provider")
			}
			if mode != "cancelled" && mode != "state-read-failure" && store.count() != 0 {
				t.Fatal("consumed callback retained usable state/result")
			}
		})
	}
}
