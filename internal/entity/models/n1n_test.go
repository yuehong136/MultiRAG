package models

import (
	"net/http"
	"testing"
	"time"
)

func TestN1NDefaultTransportLifecycle(t *testing.T) {
	original := http.DefaultTransport
	defer func() { http.DefaultTransport = original }()
	standard := &http.Transport{MaxIdleConns: 3, ResponseHeaderTimeout: time.Second}
	for _, transport := range []http.RoundTripper{standard, customProviderTransport{}, nil, (*http.Transport)(nil)} {
		http.DefaultTransport = transport
		candidate := newOpenAIChatTransport()
		if standardClone, ok := candidate.(*http.Transport); ok {
			defer standardClone.CloseIdleConnections()
		}
		if transport == standard && (candidate == standard || standard.MaxIdleConns != 3 || standard.ResponseHeaderTimeout != time.Second) {
			t.Fatal("mutated caller pool")
		}
		if _, custom := transport.(customProviderTransport); custom && candidate != transport {
			t.Fatal("custom routing dropped")
		}
		before := openAIChatTransport
		urls := map[string]string{"default": "http://controlled.invalid"}
		driver := NewN1NModel(urls, URLSuffix{Chat: "chat"})
		urls["default"] = "http://changed.invalid"
		if driver.httpClient.Transport != before || driver.httpClient.Timeout != 0 || driver.BaseURL["default"] != "http://controlled.invalid" {
			t.Fatal("N1N shared pool or instance isolation lost")
		}
	}
}
