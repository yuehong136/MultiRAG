package utility

import (
	"context"
	"errors"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"strings"
	"time"
)

var errUnsafeMCPURL = errors.New("MCP endpoint must resolve only to public HTTP addresses.")

// AssertURLSafe validates the endpoint and all DNS answers before returning a
// single pin. Resolution shares the caller's cancellation and deadline.
func AssertURLSafe(ctx context.Context, rawURL string) (string, string, error) {
	return assertURLSafe(ctx, rawURL, net.DefaultResolver.LookupHost)
}

func assertURLSafe(ctx context.Context, rawURL string, lookup func(context.Context, string) ([]string, error)) (string, string, error) {
	endpoint, err := url.Parse(rawURL)
	if err != nil || rawURL != strings.TrimSpace(rawURL) || endpoint.User != nil || endpoint.Fragment != "" || endpoint.Opaque != "" || (endpoint.Scheme != "http" && endpoint.Scheme != "https") || endpoint.Hostname() == "" {
		return "", "", errUnsafeMCPURL
	}
	host := endpoint.Hostname()
	// Keep the validated spelling identical to net/http's dial host. Unicode
	// names must be supplied as explicit ASCII IDNA labels.
	for _, char := range host {
		if char > 127 || char <= 32 {
			return "", "", errUnsafeMCPURL
		}
	}
	if strings.Contains(host, "%") || strings.HasSuffix(host, ".") {
		return "", "", errUnsafeMCPURL
	}
	if port := endpoint.Port(); port != "" {
		if _, err := net.LookupPort("tcp", port); err != nil {
			return "", "", errUnsafeMCPURL
		}
	}
	answers, err := lookup(ctx, host)
	if err != nil {
		if ctx.Err() != nil {
			return "", "", ctx.Err()
		}
		return "", "", errUnsafeMCPURL
	}
	if len(answers) == 0 {
		return "", "", errUnsafeMCPURL
	}
	first := ""
	for _, answer := range answers {
		ip, err := netip.ParseAddr(answer)
		if err != nil || !isPublicMCPAddress(ip) {
			return "", "", errUnsafeMCPURL
		}
		if first == "" {
			first = ip.Unmap().String()
		}
	}
	return host, first, nil
}

func isPublicMCPAddress(ip netip.Addr) bool {
	ip = ip.Unmap()
	if !ip.IsValid() || !ip.IsGlobalUnicast() || ip.IsPrivate() || ip.IsLoopback() || ip.IsLinkLocalUnicast() {
		return false
	}
	// Special-purpose, documentation and transition ranges are not remote
	// public services. In particular, IPv4-embedded IPv6 must not hide a pin.
	blocks := []string{"0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4", "::/96", "64:ff9b::/96", "64:ff9b:1::/48", "100::/64", "2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20"}
	for _, block := range blocks {
		if netip.MustParsePrefix(block).Contains(ip) {
			return false
		}
	}
	return ip.Is4() || netip.MustParsePrefix("2000::/3").Contains(ip)
}

// PinnedHTTPClient never falls back to system DNS, an environment proxy or an
// unvalidated redirect destination. The caller owns CloseIdleConnections.
func PinnedHTTPClient(hostname, resolvedIP string, timeout time.Duration) *http.Client {
	dialer := &net.Dialer{Timeout: timeout}
	transport := &http.Transport{
		Proxy: nil,
		DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			host, port, err := net.SplitHostPort(address)
			if err != nil || !strings.EqualFold(host, hostname) || net.ParseIP(resolvedIP) == nil {
				return nil, errUnsafeMCPURL
			}
			return dialer.DialContext(ctx, network, net.JoinHostPort(resolvedIP, port))
		},
		TLSHandshakeTimeout: timeout, ResponseHeaderTimeout: timeout,
		MaxResponseHeaderBytes: 32 << 10, DisableCompression: true,
	}
	return &http.Client{Transport: transport, Timeout: timeout, CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("MCP redirects are not allowed.") }}
}
