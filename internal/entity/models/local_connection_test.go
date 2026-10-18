package models

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sync/atomic"
	"testing"
)

func TestLocalConnectionDiscoveryHTTP(t *testing.T) {
	for _, provider := range []string{"ollama", "LM-Studio", "vllm"} {
		t.Run(provider, func(t *testing.T) {
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.Method != http.MethodGet || r.URL.Path != "/instance/models" || r.ContentLength > 0 {
					t.Errorf("invalid discovery request: %s %s", r.Method, r.URL.Path)
				}
				if auth := r.Header.Get("Authorization"); auth != "" && auth != "Bearer key" {
					t.Errorf("invalid auth: %q", auth)
				}
				if provider == "ollama" {
					fmt.Fprint(w, `{"models":[{"name":"loaded"}]}`)
				} else {
					fmt.Fprint(w, `{"data":[{"id":"loaded"}]}`)
				}
			}))
			defer server.Close()
			driver, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL + "/instance/"}, URLSuffix{Models: "/models"})
			if err != nil {
				t.Fatal(err)
			}
			for _, api := range []*APIConfig{nil, {}, {Region: stringPtr("")}, {APIKey: stringPtr("")}, {APIKey: stringPtr("key")}} {
				if err := driver.CheckConnection(api); err != nil {
					t.Fatal(err)
				}
				names, err := driver.ListModels(api)
				if err != nil || !reflect.DeepEqual(names, []string{"loaded"}) {
					t.Fatal(names, err)
				}
			}
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			if err := driver.CheckConnection(&APIConfig{Context: ctx}); err == nil {
				t.Fatal("cancellation ignored")
			}
			if calls.Load() != 10 {
				t.Fatalf("calls = %d; want 10", calls.Load())
			}
			missing, _ := NewModelFactory().CreateModelDriver(provider, nil, URLSuffix{Models: "models"})
			if err := missing.CheckConnection(nil); err == nil {
				t.Fatal("missing URL succeeded")
			}
		})
	}
}

func TestLocalConnectionRejectsInvalidDiscoveryHTTP(t *testing.T) {
	for _, provider := range []string{"ollama", "LM-Studio", "vllm"} {
		for _, body := range []string{`{}`, `{"error":"bad key"}`, `{"data":[{"id":""}],"models":[{"name":""}]}`, `invalid`} {
			t.Run(provider+body, func(t *testing.T) {
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
				defer server.Close()
				driver, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Models: "models"})
				if err := driver.CheckConnection(nil); err == nil {
					t.Fatal("invalid discovery succeeded")
				}
			})
		}
		t.Run(provider+"status", func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusUnauthorized) }))
			defer server.Close()
			driver, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Models: "models"})
			if err := driver.CheckConnection(nil); err == nil {
				t.Fatal("HTTP failure succeeded")
			}
		})
	}
}
