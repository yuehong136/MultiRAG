package service

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"multirag/internal/common"
	"multirag/internal/dao"
	rediscache "multirag/internal/engine/redis"
	"multirag/internal/entity"
	"multirag/internal/testsupport"
	"multirag/internal/testsupport/scratch"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func canonicalOAuthServices(t *testing.T) (*scratch.Services, *ConnectorService) {
	t.Helper()
	f := scratch.New(t)
	old := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = old })
	if rediscache.Get() != nil {
		t.Fatal("unexpected initialized Redis")
	}
	if err := rediscache.Init(&f.Config.Redis); err != nil {
		t.Fatal("initialize owned Redis")
	}
	t.Cleanup(func() { rediscache.Close() })
	return f, NewConnectorService()
}
func canonicalOAuthTLS(t *testing.T, handler http.Handler) *httptest.Server {
	t.Helper()
	journal := testsupport.NewCase(t)
	var listener net.Listener
	address := ""
	for range 30 {
		var token [2]byte
		if _, err := rand.Read(token[:]); err != nil {
			t.Fatal(err)
		}
		address = fmt.Sprintf("127.0.0.1:%d", 20000+int(binary.BigEndian.Uint16(token[:]))%30000)
		journal.Event("reserve-oauth-tls-listener", map[string]any{"address": address})
		var err error
		listener, err = net.Listen("tcp", address)
		if err == nil {
			break
		}
	}
	if listener == nil {
		t.Fatal("owned OAuth TLS listener unavailable")
	}
	server := &httptest.Server{Listener: listener, Config: &http.Server{Handler: handler}}
	server.StartTLS()
	t.Cleanup(func() {
		server.Close()
		connection, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
		if err == nil {
			connection.Close()
			t.Error("OAuth TLS listener survived cleanup")
		}
		journal.Event("cleanup-oauth-tls-listener", map[string]any{"address": address, "absent": err != nil})
	})
	return server
}

type canonicalOAuthTLSMapper struct {
	transport http.RoundTripper
	endpoint  *url.URL
}

func (m canonicalOAuthTLSMapper) RoundTrip(request *http.Request) (*http.Response, error) {
	if request.URL.String() != googleOAuthTokenURL {
		return nil, fmt.Errorf("unexpected OAuth token endpoint")
	}
	copy := request.Clone(request.Context())
	target := *request.URL
	target.Host, target.Scheme = m.endpoint.Host, m.endpoint.Scheme
	copy.Host = request.URL.Host
	copy.URL = &target
	return m.transport.RoundTrip(copy)
}
func TestConnectorOAuthLiveSQLRedisAndTLS(t *testing.T) {
	f, s := canonicalOAuthServices(t)
	if err := f.DB.AutoMigrate(&entity.Connector{}); err != nil {
		t.Fatal(err)
	}
	connector, err := s.CreateConnector(f.Prefix+"owner", &CreateConnectorRequest{Name: "Drive", Source: "google-drive", Config: entity.JSONMap{}})
	if err != nil {
		t.Fatal(err)
	}
	var calls atomic.Int32
	challenges := map[string]string{}
	var mu sync.Mutex
	tlsServer := canonicalOAuthTLS(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.TLS == nil || r.Method != "POST" || r.URL.Path != "/token" {
			t.Error("OAuth exchange did not use actual TLS POST")
		}
		if err := r.ParseForm(); err != nil {
			t.Error(err)
		}
		code := r.Form.Get("code")
		mu.Lock()
		expected := challenges[code]
		mu.Unlock()
		sum := sha256.Sum256([]byte(r.Form.Get("code_verifier")))
		if expected == "" || expected != base64.RawURLEncoding.EncodeToString(sum[:]) || r.Form.Get("client_secret") != "owned-client-secret" {
			t.Error("OAuth exchange lost bound PKCE/client")
		}
		fmt.Fprint(w, `{"access_token":"owned-access","refresh_token":"owned-refresh","token_type":"Bearer","expires_in":0}`)
	}))
	endpoint, _ := url.Parse(tlsServer.URL)
	oldClient := http.DefaultClient
	http.DefaultClient = &http.Client{Transport: canonicalOAuthTLSMapper{tlsServer.Client().Transport, endpoint}}
	t.Cleanup(func() { http.DefaultClient = oldClient })
	flow, code, err := s.StartGoogleWebOAuth(f.Prefix+"owner", "google-drive", &StartGoogleWebOAuthRequest{Credentials: canonicalGoogleCredentials(), RedirectURI: "https://app.invalid/callback"})
	if err != nil || code != common.CodeSuccess {
		t.Fatal("start OAuth", code, err)
	}
	authorization, _ := url.Parse(flow.AuthorizationURL)
	mu.Lock()
	challenges["owned-code"] = authorization.Query().Get("code_challenge")
	mu.Unlock()
	if flow.ExpiresIn != 900 || authorization.Query().Get("state") != flow.FlowID || strings.Contains(flow.AuthorizationURL, "owned-client-secret") {
		t.Fatal("unsafe OAuth authorization response")
	}
	if html := s.GoogleWebOAuthCallback(t.Context(), "gmail", flow.FlowID, "", "", "owned-code"); strings.Contains(html, `"status":"success"`) || calls.Load() != 0 {
		t.Fatal("wrong source consumed flow")
	}
	var wg sync.WaitGroup
	var callbackSuccess atomic.Int32
	for range 12 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if html := s.GoogleWebOAuthCallback(t.Context(), "google-drive", flow.FlowID, "", "", "owned-code"); strings.Contains(html, `"status":"success"`) {
				callbackSuccess.Add(1)
			}
		}()
	}
	wg.Wait()
	if calls.Load() != 1 || callbackSuccess.Load() != 1 {
		t.Fatal("callback state reused", calls.Load(), callbackSuccess.Load())
	}
	if result, _, err := s.PollGoogleWebOAuthResult("foreign", "google-drive", &PollGoogleWebOAuthResultRequest{FlowID: flow.FlowID}); err == nil || result != nil {
		t.Fatal("foreign actor consumed OAuth result")
	}
	var successes atomic.Int32
	var received atomic.Value
	for range 12 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			result, code, err := s.PollGoogleWebOAuthResult(f.Prefix+"owner", "google-drive", &PollGoogleWebOAuthResultRequest{FlowID: flow.FlowID})
			if err == nil && code == 0 {
				successes.Add(1)
				received.Store(result.Credentials)
			}
		}()
	}
	wg.Wait()
	if successes.Load() != 1 {
		t.Fatal("OAuth result consumed more than once", successes.Load())
	}
	credentials := received.Load().(string)
	var stored googleOAuthCredentials
	if err := json.Unmarshal([]byte(credentials), &stored); err != nil || stored.Token != "owned-access" || stored.RefreshToken != "owned-refresh" || stored.Expiry != "" {
		t.Fatal("returned OAuth credential shape/zero expiry lost")
	}
	if _, _, err := s.UpdateConnector(connector.ID, f.Prefix+"owner", &UpdateConnectorRequest{Config: entity.JSONMap{"credentials": credentials}}); err != nil {
		t.Fatal(err)
	}
	var raw string
	if err := f.Read.QueryRow("SELECT config FROM connector WHERE id=?", connector.ID).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var config map[string]interface{}
	if err := json.Unmarshal([]byte(raw), &config); err != nil || config["credentials"] != credentials {
		t.Fatal("independent connector SQL credential update readback")
	}
	if count := f.Redis.Exists(t.Context(), webStateCacheKey(flow.FlowID, "google-drive"), webResultCacheKey(flow.FlowID, "google-drive")).Val(); count != 0 {
		t.Fatal("consumed OAuth state/result survived", count)
	}
	f.Journal.Event("canonical-oauth-live-readback", map[string]any{"actual_tls_exchanges": calls.Load(), "callback_successes": callbackSuccess.Load(), "poll_successes": successes.Load(), "explicit_connector_update_readback": true})
}
func TestConnectorOAuthCancellationAndSecretSafeTokenFailure(t *testing.T) {
	f, s := canonicalOAuthServices(t)
	entered := make(chan struct{}, 1)
	server := canonicalOAuthTLS(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		r.Body.Close()
		entered <- struct{}{}
		<-r.Context().Done()
	}))
	endpoint, _ := url.Parse(server.URL)
	oldClient := http.DefaultClient
	http.DefaultClient = &http.Client{Transport: canonicalOAuthTLSMapper{server.Client().Transport, endpoint}}
	t.Cleanup(func() { http.DefaultClient = oldClient })
	flow, _, err := s.StartGoogleWebOAuth("owner", "gmail", &StartGoogleWebOAuthRequest{Credentials: canonicalGoogleCredentials()})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	done := make(chan string, 1)
	go func() { done <- s.GoogleWebOAuthCallback(ctx, "gmail", flow.FlowID, "", "", "owned-code") }()
	select {
	case <-entered:
	case <-time.After(3 * time.Second):
		t.Fatal("OAuth exchange did not start")
	}
	cancel()
	select {
	case html := <-done:
		if strings.Contains(html, `"status":"success"`) || strings.Contains(html, "owned-client-secret") {
			t.Fatal("cancelled OAuth exchange succeeded or exposed credentials")
		}
	case <-time.After(time.Second):
		t.Fatal("cancelled OAuth exchange remained blocked")
	}
	if n := f.Redis.Exists(t.Context(), webStateCacheKey(flow.FlowID, "gmail"), webResultCacheKey(flow.FlowID, "gmail")).Val(); n != 0 {
		t.Fatal("cancelled exchange left reusable state", n)
	}
}
