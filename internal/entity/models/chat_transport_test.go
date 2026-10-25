package models

import (
	"fmt"
	"net"
	"net/http"
	"net/url"
	"testing"
	"time"
)

func TestSharedChatTransportPreservesCustomDefaultRoundTripper(t *testing.T) {
	originalDefault, originalShared := http.DefaultTransport, openAIChatTransport
	defer func() { http.DefaultTransport = originalDefault; openAIChatTransport = originalShared }()
	fixture := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/embeddings" || r.Header.Get("X-Transport-Witness") != "custom" || r.Header.Get("Authorization") != "Bearer fixture" {
			t.Error("custom routing/auth contract lost", r.URL.Path)
		}
		fmt.Fprint(w, `{"data":[{"index":0,"embedding":[0,1]}]}`)
	}))
	address := fixture.Listener.Addr().String()
	t.Cleanup(func() {
		fixture.Close()
		connection, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
		if err == nil {
			connection.Close()
			t.Error("custom transport fixture listener remains")
		}
	})
	endpoint, _ := url.Parse(fixture.URL)
	underlying := &http.Transport{}
	defer underlying.CloseIdleConnections()
	calls := 0
	http.DefaultTransport = lmStudioTransportFunc(func(request *http.Request) (*http.Response, error) {
		calls++
		forward := request.Clone(request.Context())
		forward.URL.Scheme, forward.URL.Host = endpoint.Scheme, endpoint.Host
		forward.Header.Set("X-Transport-Witness", "custom")
		return underlying.RoundTrip(forward)
	})
	openAIChatTransport = newOpenAIChatTransport()
	driver := NewNovitaModel(map[string]string{"default": "http://routing.invalid"}, URLSuffix{Embedding: "embeddings"})
	data, err := driver.Embed(stringPtr("model"), []string{"q"}, &APIConfig{APIKey: stringPtr("fixture")}, &EmbeddingConfig{Dimension: 2})
	if err != nil || len(data) != 1 || len(data[0].Embedding) != 2 || calls != 1 {
		t.Fatal("actual provider request bypassed custom transport", data, err, calls)
	}
	t.Log("actual authenticated Novita request forwarded through caller routing to", fixture.URL)
}

func TestSharedChatTransportClonesStandardPool(t *testing.T) {
	original := http.DefaultTransport
	defer func() { http.DefaultTransport = original }()
	standard := &http.Transport{MaxIdleConns: 1, ResponseHeaderTimeout: time.Second}
	http.DefaultTransport = standard
	cloned, ok := newOpenAIChatTransport().(*http.Transport)
	if !ok {
		t.Fatal("standard pool type lost")
	}
	defer cloned.CloseIdleConnections()
	provider := providerHTTPTransport()
	defer provider.CloseIdleConnections()
	if provider == standard || provider.MaxIdleConns != 100 || provider.MaxIdleConnsPerHost != 10 || provider.ResponseHeaderTimeout != 60*time.Second {
		t.Fatal("provider pool clone contract lost")
	}
	if cloned == standard || standard.MaxIdleConns != 1 || standard.ResponseHeaderTimeout != time.Second || cloned.MaxIdleConns != 100 || cloned.MaxIdleConnsPerHost != 10 || cloned.ResponseHeaderTimeout != 60*time.Second {
		t.Fatal("pool cloning/mutation contract lost")
	}
}

func TestLongCatConstructorCustomDefaultTransport(t *testing.T) {
	original := http.DefaultTransport
	defer func() { http.DefaultTransport = original }()
	for _, transport := range []http.RoundTripper{customProviderTransport{}, nil, (*http.Transport)(nil)} {
		http.DefaultTransport = transport
		urls := map[string]string{"default": "http://controlled.invalid"}
		longcat := NewLongCatModel(urls, URLSuffix{Chat: "chat"})
		voyage := NewVoyageModel(urls, URLSuffix{Embedding: "embed"})
		urls["default"] = "http://changed.invalid"
		if longcat.httpClient.Transport != openAIChatTransport || voyage.httpClient.Transport != providerModelTransport || longcat.BaseURL["default"] != "http://controlled.invalid" || voyage.BaseURL["default"] != "http://controlled.invalid" {
			t.Fatal("shared pool or instance isolation lost")
		}
		// Replacing the global default after startup must not reinterpret existing pools.
		fallback := providerHTTPTransport()
		if fallback.DialContext == nil || fallback.TLSHandshakeTimeout != 10*time.Second || fallback.ResponseHeaderTimeout != 60*time.Second || fallback.MaxIdleConnsPerHost != 10 {
			t.Fatal("custom-default fallback lost deadlines")
		}
		fallback.CloseIdleConnections()
		if transport == nil {
			pool, ok := newOpenAIChatTransport().(*http.Transport)
			if !ok || pool == nil {
				t.Fatal("nil-default fallback")
			}
			pool.CloseIdleConnections()
		}
	}
}

func TestLongCatCustomTransportRequestsAndIsolation(t *testing.T) {
	originalDefault, originalShared := http.DefaultTransport, openAIChatTransport
	defer func() { http.DefaultTransport = originalDefault; openAIChatTransport = originalShared }()
	fixture := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Transport-Witness") != "custom" || r.Header.Get("Authorization") != "Bearer key" {
			t.Error("routing/auth lost")
		}
		fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
	}))
	endpoint, _ := url.Parse(fixture.URL)
	underlying := &http.Transport{}
	defer underlying.CloseIdleConnections()
	calls := 0
	http.DefaultTransport = lmStudioTransportFunc(func(r *http.Request) (*http.Response, error) {
		calls++
		forward := r.Clone(r.Context())
		forward.URL.Scheme, forward.URL.Host = endpoint.Scheme, endpoint.Host
		forward.Header.Set("X-Transport-Witness", "custom")
		return underlying.RoundTrip(forward)
	})
	openAIChatTransport = newOpenAIChatTransport()
	urls := map[string]string{"default": "http://routing.invalid"}
	first := NewLongCatModel(urls, URLSuffix{Chat: "chat"})
	second := NewLongCatModel(urls, URLSuffix{Chat: "chat"})
	urls["default"] = "http://changed.invalid"
	if first.httpClient == second.httpClient {
		t.Fatal("clients/pool isolation")
	}
	for _, m := range []*LongCatModel{first, second} {
		if _, err := m.ChatWithMessages("model", &APIConfig{APIKey: stringPtr("key")}, []Message{{Role: "user", Content: "q"}}, nil); err != nil {
			t.Fatal(err)
		}
	}
	if calls != 2 {
		t.Fatal("custom transport bypassed", calls)
	}
}
