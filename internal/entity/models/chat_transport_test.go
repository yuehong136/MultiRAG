package models

import (
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
	"time"
)

func TestSharedChatTransportPreservesCustomDefaultRoundTripper(t *testing.T) {
	originalDefault, originalShared := http.DefaultTransport, openAIChatTransport
	defer func() { http.DefaultTransport = originalDefault; openAIChatTransport = originalShared }()
	fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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
	if cloned == standard || standard.MaxIdleConns != 1 || standard.ResponseHeaderTimeout != time.Second || cloned.MaxIdleConns != 100 || cloned.MaxIdleConnsPerHost != 10 || cloned.ResponseHeaderTimeout != 60*time.Second {
		t.Fatal("pool cloning/mutation contract lost")
	}
}
