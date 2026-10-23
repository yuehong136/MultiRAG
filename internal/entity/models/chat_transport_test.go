package models

import (
	"errors"
	"net/http"
	"testing"
	"time"
)

func TestSharedChatTransportAcceptsCustomDefaultRoundTripper(t *testing.T) {
	original := http.DefaultTransport
	defer func() { http.DefaultTransport = original }()
	http.DefaultTransport = lmStudioTransportFunc(func(*http.Request) (*http.Response, error) { return nil, errors.New("custom transport") })
	transport := newOpenAIChatTransport()
	defer transport.CloseIdleConnections()
	if transport.Proxy == nil || transport.MaxIdleConns != 100 || transport.MaxIdleConnsPerHost != 10 || transport.ResponseHeaderTimeout != 60*time.Second {
		t.Fatal("fallback lost bounded connection-pool configuration")
	}
	custom := &http.Transport{MaxIdleConns: 1, ResponseHeaderTimeout: time.Second}
	http.DefaultTransport = custom
	cloned := newOpenAIChatTransport()
	defer cloned.CloseIdleConnections()
	if cloned == custom || custom.MaxIdleConns != 1 || custom.ResponseHeaderTimeout != time.Second {
		t.Fatal("default transport mutated")
	}
}
