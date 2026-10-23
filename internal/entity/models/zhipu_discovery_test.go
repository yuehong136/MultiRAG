package models

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

func TestZhipuDiscovery(t *testing.T) {
	for _, key := range []string{"", "fixture"} {
		t.Run(key, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/v4/models" || r.Method != "GET" {
					t.Errorf("request %s %s", r.Method, r.URL.Path)
				}
				auth := ""
				if key != "" {
					auth = "Bearer " + key
				}
				if r.Header.Get("Authorization") != auth {
					t.Error("auth mismatch")
				}
				_, _ = w.Write([]byte(`{"data":[{"id":"glm"},{"id":"embedding","owned_by":"zhipu"}]}`))
			}))
			defer server.Close()
			driver := NewZhipuAIModel(map[string]string{"default": server.URL + "/v4/"}, URLSuffix{Models: "models"})
			got, err := driver.ListModels(&APIConfig{APIKey: &key})
			if err != nil || !reflect.DeepEqual(got, []string{"glm", "embedding@zhipu"}) {
				t.Fatalf("%v %v", got, err)
			}
			region := "unknown"
			if _, err = driver.ListModels(&APIConfig{Region: &region}); err == nil {
				t.Fatal("unknown region accepted")
			}
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			if _, err = driver.ListModels(&APIConfig{Context: ctx}); !errors.Is(err, context.Canceled) {
				t.Fatalf("cancel: %v", err)
			}
		})
	}
	for _, body := range []string{`{}`, `{"data":null}`, `{"data":[{}]}`, `{"data":[],"error":{"message":"failed"}}`, `{"data":[]} {}`} {
		t.Run(body, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(body)) }))
			defer srv.Close()
			d := NewZhipuAIModel(map[string]string{"default": srv.URL}, URLSuffix{Models: "models"})
			if _, err := d.ListModels(nil); err == nil {
				t.Fatal("invalid discovery accepted")
			}
		})
	}
}
