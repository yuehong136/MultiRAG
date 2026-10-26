package models

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sync/atomic"
	"testing"
	"time"
)

func TestAfterB7TransportRoutingAndIsolation(t *testing.T) {
	originalDefault, originalShared := http.DefaultTransport, openAIChatTransport
	defer func() { http.DefaultTransport = originalDefault; openAIChatTransport = originalShared }()
	var calls atomic.Int32
	fixture := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("X-Transport-Witness") != "wrapper" || r.Header.Get("Authorization") != "Bearer key" || r.URL.Path != "/chat" {
			t.Error("routing/auth", r.Header, r.URL)
		}
		io.Copy(io.Discard, r.Body)
		if r.Header.Get("X-Wait") == "true" {
			w.WriteHeader(200)
			w.(http.Flusher).Flush()
			<-r.Context().Done()
			return
		}
		fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
	}))
	endpoint, _ := url.Parse(fixture.URL)
	underlying := &http.Transport{}
	defer underlying.CloseIdleConnections()
	wrapper := lmStudioTransportFunc(func(r *http.Request) (*http.Response, error) {
		forward := r.Clone(r.Context())
		forward.URL.Scheme, forward.URL.Host = endpoint.Scheme, endpoint.Host
		forward.Header.Set("X-Transport-Witness", "wrapper")
		if deadline, ok := r.Context().Deadline(); ok && time.Until(deadline) < time.Second {
			forward.Header.Set("X-Wait", "true")
		}
		return underlying.RoundTrip(forward)
	})
	http.DefaultTransport = &struct{ http.RoundTripper }{wrapper}
	openAIChatTransport = newOpenAIChatTransport()
	for _, provider := range []string{"ModelScope", "Novita"} {
		t.Run(provider, func(t *testing.T) {
			urls := map[string]string{"default": "http://routing.invalid"}
			first, _ := NewModelFactory().CreateModelDriver(provider, urls, URLSuffix{Chat: "chat"})
			second, _ := NewModelFactory().CreateModelDriver(provider, urls, URLSuffix{Chat: "chat"})
			var a, b *openAIChatDriver
			if provider == "Novita" {
				a, b = first.(*NovitaModel).openAIChatDriver, second.(*NovitaModel).openAIChatDriver
			} else {
				a, b = first.(*ModelScopeModel).openAIChatDriver, second.(*ModelScopeModel).openAIChatDriver
			}
			urls["default"] = "http://mutated.invalid"
			a.BaseURL["other"] = "http://other.invalid"
			if a.httpClient == b.httpClient || a.BaseURL["default"] != "http://routing.invalid" || b.BaseURL["other"] != "" || a.httpClient.Transport != openAIChatTransport || b.httpClient.Transport != openAIChatTransport || a.httpClient.Timeout != 0 {
				t.Fatal("client/map/pool isolation")
			}
			before := calls.Load()
			for _, driver := range []ModelDriver{first, second} {
				if got, err := driver.ChatWithMessages("model", &APIConfig{APIKey: stringPtr("key")}, []Message{{Role: "user", Content: "q"}}, nil); err != nil || *got.Answer != "answer" {
					t.Fatal(got, err)
				}
				ctx, cancel := context.WithTimeout(t.Context(), 50*time.Millisecond)
				_, err := driver.ChatWithMessages("model", &APIConfig{APIKey: stringPtr("key"), Context: ctx}, []Message{{Role: "user", Content: "q"}}, nil)
				cancel()
				if !errors.Is(err, context.DeadlineExceeded) {
					t.Fatal("wrapper lost deadline", err)
				}
			}
			if calls.Load()-before != 4 {
				t.Fatal("actual wrapper requests", calls.Load()-before)
			}
		})
	}
}

func TestAfterB7TransportStandardCloneProxy(t *testing.T) {
	originalDefault, originalShared := http.DefaultTransport, openAIChatTransport
	defer func() { http.DefaultTransport = originalDefault; openAIChatTransport = originalShared }()
	var proxyCalls atomic.Int32
	proxy := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		proxyCalls.Add(1)
		if r.URL.Host != "provider.invalid" || r.Header.Get("Authorization") != "Bearer key" {
			t.Error("proxy/auth", r.URL, r.Header)
		}
		fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
	}))
	proxyURL, _ := url.Parse(proxy.URL)
	standard := &http.Transport{Proxy: http.ProxyURL(proxyURL), MaxIdleConns: 1, ResponseHeaderTimeout: time.Second, TLSHandshakeTimeout: 7 * time.Second}
	http.DefaultTransport = standard
	pool := newOpenAIChatTransport().(*http.Transport)
	defer pool.CloseIdleConnections()
	openAIChatTransport = pool
	if pool == standard || standard.MaxIdleConns != 1 || pool.MaxIdleConns != 100 || pool.MaxIdleConnsPerHost != 10 || pool.ResponseHeaderTimeout != 60*time.Second || pool.TLSHandshakeTimeout != 7*time.Second {
		t.Fatal("clone contract")
	}
	for _, provider := range []string{"ModelScope", "Novita"} {
		driver, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": "http://provider.invalid"}, URLSuffix{Chat: "chat"})
		if _, err := driver.ChatWithMessages("model", &APIConfig{APIKey: stringPtr("key")}, []Message{{Role: "user", Content: "q"}}, nil); err != nil {
			t.Fatal(provider, err)
		}
	}
	if proxyCalls.Load() != 2 {
		t.Fatal("standard proxy bypassed", proxyCalls.Load())
	}
}
