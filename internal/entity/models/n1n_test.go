package models

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestN1NDiscoveryUsesConfiguredEndpointAndRegion(t *testing.T) {
	var calls atomic.Int32
	server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		wantKey := map[string]string{"/regional/models": "regional-key", "/one/models": "one-key", "/two/models": "two-key"}[r.URL.Path]
		if wantKey == "" || r.Method != http.MethodGet || r.ContentLength > 0 || r.Header.Get("Authorization") != "Bearer "+wantKey {
			t.Error("configured discovery identity lost", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		fmt.Fprint(w, `{"data":[{"id":" served ","owned_by":"native"},{"id":""},{"id":"served"}]}`)
	}))
	defer server.Close()
	driver := NewN1NModel(map[string]string{"default": server.URL + "/catalog", "regional": server.URL + "/regional"}, URLSuffix{Models: "/models"})
	for _, test := range []struct {
		region, endpoint, key string
	}{
		{"regional", "", "regional-key"},
		{"missing", server.URL + "/one/", "one-key"},
		{"missing", server.URL + "/two", "two-key"},
	} {
		wantConfig := test
		api := &APIConfig{Region: &test.region, BaseURL: &test.endpoint, ApiKey: &test.key}
		models, err := driver.ListModels(api)
		if err != nil || joinModelNames(models, "|") != "served@native|served" {
			t.Fatal(models, err)
		}
		if err := driver.CheckConnection(api); err != nil {
			t.Fatal("connection ignored the configured discovery endpoint", err)
		}
		if *api.BaseURL != wantConfig.endpoint || *api.Region != wantConfig.region || *api.ApiKey != wantConfig.key {
			t.Fatal("caller configuration mutated")
		}
	}
	if calls.Load() != 6 || driver.baseModel.BaseURL["default"] != server.URL+"/catalog" {
		t.Fatal("discovery configuration crossed instances", calls.Load())
	}
	before := calls.Load()
	for _, api := range []*APIConfig{
		{ApiKey: stringPtr("key"), Region: stringPtr("missing")},
		{ApiKey: stringPtr("key"), BaseURL: stringPtr("ftp://invalid.example")},
		{ApiKey: stringPtr("key"), BaseURL: stringPtr("https://user:secret@invalid.example")},
		{BaseURL: stringPtr(server.URL + "/one")},
	} {
		if result, err := driver.ListModels(api); err == nil || result != nil || strings.Contains(err.Error(), "secret") {
			t.Fatal("invalid configuration accepted", result, err)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := driver.ListModels(&APIConfig{ApiKey: stringPtr("one-key"), BaseURL: stringPtr(server.URL + "/one"), Context: ctx}); !errors.Is(err, context.Canceled) {
		t.Fatal("caller cancellation lost", err)
	}
	if calls.Load() != before {
		t.Fatal("invalid or canceled discovery reached provider HTTP", calls.Load())
	}
}

func TestN1NDefaultTransportLifecycle(t *testing.T) {
	original := http.DefaultTransport
	defer func() { http.DefaultTransport = original }()
	standard := &http.Transport{MaxIdleConns: 3, ResponseHeaderTimeout: time.Second}
	for _, transport := range []http.RoundTripper{standard, customProviderTransport{}, nil, (*http.Transport)(nil)} {
		http.DefaultTransport = transport
		candidate := NewDriverHTTPClient().Transport
		if standardClone, ok := candidate.(*http.Transport); ok {
			defer standardClone.CloseIdleConnections()
		}
		if transport == standard && (candidate == standard || standard.MaxIdleConns != 3 || standard.ResponseHeaderTimeout != time.Second) {
			t.Fatal("mutated caller pool")
		}
		if _, custom := transport.(customProviderTransport); custom && candidate != transport {
			t.Fatal("custom routing dropped")
		}

		urls := map[string]string{"default": "http://controlled.invalid"}
		driver := NewN1NModel(urls, URLSuffix{Chat: "chat"})
		urls["default"] = "http://changed.invalid"
		if driver.baseModel.httpClient.Timeout != 0 || driver.baseModel.BaseURL["default"] != "http://controlled.invalid" {
			t.Fatal("N1N shared pool or instance isolation lost")
		}
	}
}
