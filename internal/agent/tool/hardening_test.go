package tool

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestHTTPHelperDoesNotRetryPOST(t *testing.T) {
	var hits atomic.Int32
	srv := toolTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hits.Add(1); w.WriteHeader(503) }))
	_, err := NewHTTPHelper().Do(context.Background(), http.MethodPost, srv.URL, "charge", "", nil)
	if err == nil || hits.Load() != 1 {
		t.Fatalf("POST outcome: %v, hits=%d", err, hits.Load())
	}
}
func TestHTTPHelperBoundsResponse(t *testing.T) {
	srv := toolTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, strings.Repeat("x", maxToolResponseBytes+1))
	}))
	_, err := NewHTTPHelper().Do(context.Background(), http.MethodGet, srv.URL, "", "", nil)
	if err == nil || !strings.Contains(err.Error(), "exceeds limit") {
		t.Fatalf("oversize response: %v", err)
	}
}
func TestHTTPHelperPinnedRedirectRequiresValidation(t *testing.T) {
	var hits atomic.Int32
	dest := toolTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hits.Add(1) }))
	src := toolTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, dest.URL, 302) }))
	u, _ := url.Parse(src.URL)
	response, err := NewHTTPHelper().DoPinned(context.Background(), http.MethodGet, src.URL, "", "", nil, u.Hostname(), net.ParseIP(u.Hostname()))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != 302 || hits.Load() != 0 {
		t.Fatalf("followed redirect: %d hits=%d", response.StatusCode, hits.Load())
	}
}
func TestCrawlerRejectsHTTPFailure(t *testing.T) {
	srv := toolTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(404)
		_, _ = io.WriteString(w, "<html>failure</html>")
	}))
	crawler := NewCrawlerTool().WithResolver(func(raw string) (string, net.IP, error) {
		u, _ := url.Parse(raw)
		return u.Hostname(), net.ParseIP(u.Hostname()), nil
	})
	args, _ := json.Marshal(crawlerArgs{URL: srv.URL})
	out, err := crawler.InvokableRun(context.Background(), string(args))
	if err == nil || !strings.Contains(out, "_ERROR") {
		t.Fatalf("failure page became success: %s %v", out, err)
	}
}
func TestHTTPHelperRedactsTransportURLError(t *testing.T) {
	want := context.Canceled
	helper := NewHTTPHelper().WithClient(&http.Client{Transport: toolRoundTripFunc(func(*http.Request) (*http.Response, error) {
		return nil, &url.Error{Op: "Get", URL: "https://provider.invalid/?api_key=secret", Err: want}
	})})
	_, err := helper.Do(context.Background(), "GET", "https://provider.invalid/?api_key=secret", "", "", nil)
	if !errors.Is(err, want) || strings.Contains(err.Error(), "secret") {
		t.Fatalf("secret leak or lost cancellation: %v", err)
	}
}
func TestEmailCancelsGreeting(t *testing.T) {
	ln, err := toolTestListener(t)
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		buf := make([]byte, 1)
		_, _ = conn.Read(buf)
	}()
	host, port, _ := net.SplitHostPort(ln.Addr().String())
	var number int
	for _, r := range port {
		number = number*10 + int(r-'0')
	}
	args, _ := json.Marshal(emailParams{SMTPHost: host, SMTPPort: number, FromAddr: "a@b", ToAddrs: []string{"c@d"}, Subject: "subject", Body: "body"})
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	start := time.Now()
	_, err = NewEmailTool().WithResolver(toolTestResolver).InvokableRun(ctx, string(args))
	if !errors.Is(err, context.DeadlineExceeded) || time.Since(start) > time.Second {
		t.Fatalf("SMTP did not cancel: %v", err)
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("SMTP connection remains")
	}
}
func TestEmailRejectsHeaderInjection(t *testing.T) {
	for _, params := range []emailParams{
		{SMTPHost: "smtp.example", SMTPPort: 587, FromAddr: "a@b", ToAddrs: []string{"c@d"}, Subject: "s\r\nBcc: attacker@example.com"},
		{SMTPHost: "smtp.example", SMTPPort: 587, FromAddr: "a@b\r\nX: injected", ToAddrs: []string{"c@d"}, Subject: "s"},
	} {
		if err := validateEmailParams(&params); err == nil {
			t.Fatal("header injection accepted")
		}
	}
}
func TestResolveCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, _, err := ResolveAndValidateContext(ctx, "https://example.invalid")
	if !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}

type toolRoundTripFunc func(*http.Request) (*http.Response, error)

func (f toolRoundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestDecodeToolJSONRejectsFalseSuccess(t *testing.T) {
	for _, payload := range []string{`null`, `[]`, `{"error":{"message":"secret"}}`, `{} {}`, `{} garbage`} {
		var out map[string]any
		if err := decodeToolJSON(strings.NewReader(payload), &out); err == nil {
			t.Errorf("accepted %s", payload)
		}
	}
}
func TestExeSQLRegistryRejectsInvalidNumericConfig(t *testing.T) {
	for _, value := range []any{3.5, -1, "3306", 1e99} {
		_, err := BuildByName("execute_sql", map[string]any{"db_type": "mysql", "host": "example.com", "port": value, "database": "db", "username": "user"})
		if err == nil {
			t.Errorf("accepted port %v", value)
		}
	}
}
