package models

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"strings"
)

func paddleSameOrigin(a, b *url.URL) bool {
	return a.Scheme == b.Scheme && strings.EqualFold(a.Host, b.Host)
}

func paddlePublicIP(ip net.IP) bool {
	if !ip.IsGlobalUnicast() || ip.IsPrivate() || ip.IsLoopback() || ip.IsLinkLocalUnicast() {
		return false
	}
	address, ok := netip.AddrFromSlice(ip)
	if !ok {
		return false
	}
	address = address.Unmap()
	for _, prefix := range []string{"100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4"} {
		if netip.MustParsePrefix(prefix).Contains(address) {
			return false
		}
	}
	return true
}

func validatePaddleResultURL(base, target *url.URL) error {
	if target == nil || target.Scheme != "http" && target.Scheme != "https" || target.Host == "" || target.User != nil || target.Fragment != "" || len(target.String()) > 8192 {
		return fmt.Errorf("invalid PaddleOCR result URL")
	}
	if paddleSameOrigin(base, target) {
		return nil
	}
	// Public cross-origin object storage is supported; private/self-hosted results
	// must remain on the explicitly configured provider origin.
	if target.Scheme != "https" {
		return fmt.Errorf("cross-origin PaddleOCR results require HTTPS")
	}
	if ip := net.ParseIP(target.Hostname()); ip != nil && !paddlePublicIP(ip) {
		return fmt.Errorf("private PaddleOCR result destination")
	}
	return nil
}

func (p *PaddleOCRModel) downloadResult(endpoint, resultURL string, api *APIConfig) (*OCRResponse, error) {
	base, _ := url.Parse(endpoint)
	target, err := url.Parse(resultURL)
	if err != nil || resultURL == "" {
		return nil, fmt.Errorf("invalid PaddleOCR result URL")
	}
	if err := validatePaddleResultURL(base, target); err != nil {
		return nil, err
	}
	client := *p.httpClient
	client.Jar = nil
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if existing, ok := client.Transport.(*http.Transport); ok {
		transport = existing.Clone()
	}
	transport.Proxy = nil
	trustedPort := base.Port()
	if trustedPort == "" {
		if base.Scheme == "https" {
			trustedPort = "443"
		} else {
			trustedPort = "80"
		}
	}
	transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
		host, port, err := net.SplitHostPort(address)
		if err != nil {
			return nil, fmt.Errorf("invalid result destination")
		}
		dialer := net.Dialer{}
		if strings.EqualFold(host, base.Hostname()) && port == trustedPort {
			return dialer.DialContext(ctx, network, address)
		}
		addresses, err := net.DefaultResolver.LookupIPAddr(ctx, host)
		if err != nil || len(addresses) == 0 {
			return nil, fmt.Errorf("result hostname resolution failed")
		}
		for _, address := range addresses {
			if !paddlePublicIP(address.IP) {
				return nil, fmt.Errorf("private PaddleOCR result destination")
			}
		}
		// Dial the validated IP directly, preventing a second DNS lookup/rebinding.
		for _, address := range addresses {
			connection, err := dialer.DialContext(ctx, network, net.JoinHostPort(address.IP.String(), port))
			if err == nil {
				return connection, nil
			}
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
		}
		return nil, fmt.Errorf("PaddleOCR result connection failed")
	}
	defer transport.CloseIdleConnections()
	client.Transport = transport
	client.CheckRedirect = func(req *http.Request, via []*http.Request) error {
		if len(via) > 3 {
			return fmt.Errorf("too many result redirects")
		}
		req.Header.Del("Authorization")
		req.Header.Del("Cookie")
		return validatePaddleResultURL(base, req.URL)
	}
	req, err := http.NewRequestWithContext(requestContext(api), http.MethodGet, resultURL, nil)
	if err != nil {
		return nil, fmt.Errorf("invalid PaddleOCR result request")
	}
	resp, err := client.Do(req)
	if err != nil {
		if err := requestContext(api).Err(); err != nil {
			return nil, err
		}
		return nil, fmt.Errorf("PaddleOCR result download failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("PaddleOCR result download failed (HTTP %d)", resp.StatusCode)
	}
	result, err := readPaddleJSONL(resp.Body)
	if err != nil {
		return nil, err
	}
	if err := requestContext(api).Err(); err != nil {
		return nil, err
	}
	return result, nil
}
