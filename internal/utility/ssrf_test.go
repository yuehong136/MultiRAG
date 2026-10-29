package utility

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"sync/atomic"
	"testing"
	"time"
)

func TestMCPSSRFAllAnswersAndURLShape(t *testing.T) {
	for _, address := range []string{"127.0.0.1", "10.0.0.1", "172.16.1.1", "192.168.1.1", "100.64.0.1", "192.0.2.1", "198.51.100.1", "203.0.113.1", "198.18.0.1", "240.0.0.1", "::1", "::ffff:127.0.0.1", "fe80::1", "fc00::1", "2001:db8::1", "64:ff9b::7f00:1", "2002:7f00:1::1", "2001::1", "3fff::1"} {
		_, _, err := assertURLSafe(context.Background(), "https://public.example/path", func(context.Context, string) ([]string, error) { return []string{"8.8.8.8", address}, nil })
		if err == nil {
			t.Fatal("unsafe mixed answer accepted", address)
		}
	}
	for _, endpoint := range []string{"file:///tmp/data", "ftp://example.com", "https:///path", "https://user:secret@example.com", "https://example.com/#secret", " https://example.com", "https://example.com.", "http://[fe80::1%25en0]/", "https://例子.example"} {
		_, _, err := assertURLSafe(context.Background(), endpoint, func(context.Context, string) ([]string, error) { return []string{"8.8.8.8"}, nil })
		if err == nil {
			t.Fatal("unsafe URL accepted", endpoint)
		}
	}
	host, ip, err := assertURLSafe(context.Background(), "https://example.com/path?token=value", func(context.Context, string) ([]string, error) {
		return []string{"8.8.8.8", "2606:4700:4700::1111"}, nil
	})
	if err != nil || host != "example.com" || ip != "8.8.8.8" {
		t.Fatal("public endpoint rejected", err)
	}
}

func TestMCPSSRFCancellationAndPinnedHost(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, _, err := assertURLSafe(ctx, "http://example.com", func(ctx context.Context, _ string) ([]string, error) { return nil, ctx.Err() })
	if !errors.Is(err, context.Canceled) {
		t.Fatal("DNS ignored cancellation", err)
	}
	client := PinnedHTTPClient("example.com", "8.8.8.8", time.Second)
	defer client.CloseIdleConnections()
	transport := client.Transport.(*http.Transport)
	if transport.Proxy != nil {
		t.Fatal("proxy can bypass pin")
	}
	if _, err := transport.DialContext(context.Background(), "tcp", "different.example:80"); err == nil {
		t.Fatal("cross-host fallback remained")
	}
	if client.CheckRedirect == nil || client.CheckRedirect(&http.Request{}, nil) == nil {
		t.Fatal("redirect remained enabled")
	}
}

// A loopback pin is supplied only to exercise the transport with controlled
// listeners. Production FetchTools still resolves and rejects private addresses.
func TestMCPSSRFPinnedTCPHostProxyAndRedirectCredentials(t *testing.T) {
	var otherRequests atomic.Int32
	var otherCredentials atomic.Int32
	other := mcpTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		otherRequests.Add(1)
		if r.Header.Get("X-Custom-Secret") != "" {
			otherCredentials.Add(1)
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	// If environment proxies were consulted, even the initial request would
	// reach this separately registered listener instead of the chosen pin.
	t.Setenv("HTTP_PROXY", other.URL)
	t.Setenv("HTTPS_PROXY", other.URL)
	t.Setenv("ALL_PROXY", other.URL)
	t.Setenv("NO_PROXY", "")
	hosts := make(chan string, 4)
	var sameOriginDestinationRequests atomic.Int32
	pinned := mcpTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hosts <- r.Host
		if r.Header.Get("X-Custom-Secret") != "controlled-test-secret" {
			t.Error("custom header missing at pinned origin")
		}
		switch r.URL.Path {
		case "/":
			io.WriteString(w, "pinned")
		case "/same-origin":
			http.Redirect(w, r, "/destination", http.StatusFound)
		case "/cross-origin":
			http.Redirect(w, r, other.URL+"/destination", http.StatusFound)
		default:
			sameOriginDestinationRequests.Add(1)
			w.WriteHeader(http.StatusNoContent)
		}
	}))
	_, port, err := net.SplitHostPort(pinned.Listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	host := "mcp.invalid:" + port
	endpoint := "http://" + host
	client := PinnedHTTPClient("mcp.invalid", "127.0.0.1", time.Second)
	defer client.CloseIdleConnections()
	for _, path := range []string{"/", "/same-origin", "/cross-origin"} {
		request, err := http.NewRequestWithContext(context.Background(), http.MethodGet, endpoint+path, nil)
		if err != nil {
			t.Fatal(err)
		}
		request.Header.Set("X-Custom-Secret", "controlled-test-secret")
		response, err := client.Do(request)
		if path == "/" {
			if err != nil {
				t.Fatal("request did not reach the exact DNS pin")
			}
			body, readErr := io.ReadAll(io.LimitReader(response.Body, 16))
			response.Body.Close()
			if readErr != nil || response.StatusCode != http.StatusOK || string(body) != "pinned" {
				t.Fatal("unexpected pinned response")
			}
		} else {
			if response != nil {
				response.Body.Close()
			}
			if err == nil {
				t.Fatal("redirect accepted", path)
			}
		}
		select {
		case got := <-hosts:
			if got != host {
				t.Fatal("pin changed the HTTP Host")
			}
		case <-time.After(time.Second):
			t.Fatal("pinned listener did not receive request")
		}
	}
	if sameOriginDestinationRequests.Load() != 0 || otherRequests.Load() != 0 || otherCredentials.Load() != 0 {
		t.Fatal("redirect or proxy reached a forbidden destination")
	}
}
