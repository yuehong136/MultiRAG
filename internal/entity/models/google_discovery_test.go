package models

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestGoogleDiscoveryChecksKeysWithoutHTTP(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); fmt.Fprint(w, `{"models":[]}`) }))
	defer server.Close()
	driver := NewGoogleModel(map[string]string{"default": server.URL}, URLSuffix{})
	for _, api := range []*APIConfig{nil, {}, {APIKey: stringPtr("")}, {APIKey: stringPtr(" \t\n ")}} {
		if names, err := driver.ListModels(api); err == nil || names != nil {
			t.Fatalf("invalid key accepted: %v %v", names, err)
		}
		if err := driver.CheckConnection(api); err == nil {
			t.Fatal("connection check accepted invalid key")
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := driver.CheckConnection(&APIConfig{APIKey: stringPtr("key"), Context: ctx}); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if calls.Load() != 0 {
		t.Fatalf("invalid configuration made %d requests", calls.Load())
	}
}

func TestGoogleConnectionPropagatesLaterPageFailure(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("x-goog-api-key") != "fixture-key" {
			t.Error("list authentication lost")
		}
		if r.URL.Query().Get("pageToken") == "" {
			fmt.Fprint(w, `{"models":[{"name":"first"}],"nextPageToken":"second"}`)
			return
		}
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"error":{"code":403,"message":"fixture-key secret provider body"}}`)
	}))
	defer server.Close()
	driver := NewGoogleModel(map[string]string{"default": server.URL}, URLSuffix{})
	err := driver.CheckConnection(&APIConfig{APIKey: stringPtr("fixture-key")})
	if err == nil || !strings.Contains(err.Error(), "403") || strings.Contains(err.Error(), "fixture-key") || strings.Contains(err.Error(), "secret") || calls.Load() != 2 {
		t.Fatalf("connection %v, calls %d", err, calls.Load())
	}
}

func TestGoogleDiscoveryCancellationReachesLaterPage(t *testing.T) {
	started, cancelled := make(chan struct{}), make(chan struct{})
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Query().Get("pageToken") == "" {
			fmt.Fprint(w, `{"models":[{"name":"first"}],"nextPageToken":"second"}`)
			return
		}
		w.WriteHeader(http.StatusOK)
		w.(http.Flusher).Flush()
		close(started)
		<-r.Context().Done()
		close(cancelled)
	}))
	defer server.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	driver := NewGoogleModel(map[string]string{"default": server.URL}, URLSuffix{})
	done := make(chan error, 1)
	go func() {
		names, err := driver.ListModels(&APIConfig{APIKey: stringPtr("key"), Context: ctx})
		if names != nil {
			t.Error("partial listing returned after cancellation")
		}
		done <- err
	}()
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("second page did not start")
	}
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("listing did not cancel")
	}
	select {
	case <-cancelled:
	case <-time.After(5 * time.Second):
		t.Fatal("provider did not observe cancellation")
	}
	if calls.Load() != 2 {
		t.Fatalf("list calls %d", calls.Load())
	}
}

func TestGoogleConstructorOwnsURLMap(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); fmt.Fprint(w, `{"models":[]}`) }))
	defer server.Close()
	urls := map[string]string{"default": server.URL}
	driver := NewGoogleModel(urls, URLSuffix{})
	urls["default"] = "http://127.0.0.1:1"
	if err := driver.CheckConnection(&APIConfig{APIKey: stringPtr("key")}); err != nil || calls.Load() != 1 {
		t.Fatalf("constructor retained caller map: %v", err)
	}
}

func TestGoogleDiscoveryRejectsMalformedResponses(t *testing.T) {
	for _, body := range []string{
		`null`, `42`, `{}`, `{"models":null}`, `{"models":"invalid"}`, `{"models":[null]}`,
		`{"models":[{}]}`, `{"models":[{"name":null}]}`, `{"models":[{"name":""}]}`,
		`{"models":[{"name":"  \t "}]}`, `{"models":[{"name":42}]}`,
		`{"error":{"code":403,"message":"fixture-key private error"}}`,
	} {
		t.Run(body, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
			defer server.Close()
			driver := NewGoogleModel(map[string]string{"default": server.URL}, URLSuffix{})
			names, err := driver.ListModels(&APIConfig{APIKey: stringPtr("fixture-key")})
			if names != nil || err == nil || strings.Contains(err.Error(), "fixture-key") || strings.Contains(err.Error(), "private error") {
				t.Fatalf("bad listing succeeded or leaked: %v %v", names, err)
			}
			if err := driver.CheckConnection(&APIConfig{APIKey: stringPtr("fixture-key")}); err == nil || strings.Contains(err.Error(), "fixture-key") || strings.Contains(err.Error(), "private error") {
				t.Fatalf("bad inventory passed connection check or leaked: %v", err)
			}
		})
	}
}

func TestGoogleDiscoveryAcceptsExplicitEmptyInventory(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, `{"models":[]}`) }))
	defer server.Close()
	driver := NewGoogleModel(map[string]string{"default": server.URL}, URLSuffix{})
	api := &APIConfig{APIKey: stringPtr("fixture-key")}
	if names, err := driver.ListModels(api); err != nil || names == nil || len(names) != 0 {
		t.Fatalf("valid empty inventory: %v %v", names, err)
	}
	if err := driver.CheckConnection(api); err != nil {
		t.Fatal(err)
	}
}
