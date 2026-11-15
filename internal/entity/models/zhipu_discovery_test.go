package models

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"reflect"
	"strings"
	"testing"
)

func TestZhipuDiscovery(t *testing.T) {
	for _, key := range []string{"fixture"} {
		t.Run(key, func(t *testing.T) {
			server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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
			driver := NewZhipuAIModel(map[string]string{"default": server.URL + "/v4"}, URLSuffix{Models: "models"})
			got, err := driver.ListModels(&APIConfig{ApiKey: &key})
			if err != nil || !reflect.DeepEqual(modelNamesForTest(got), []string{"glm", "embedding@zhipu"}) {
				t.Fatalf("%v %v", got, err)
			}
			region := "unknown"
			if _, err = driver.ListModels(&APIConfig{Region: &region}); err == nil {
				t.Fatal("unknown region accepted")
			}
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			if _, err = driver.ListModels(&APIConfig{ApiKey: &key, Context: ctx}); !errors.Is(err, context.Canceled) {
				t.Fatalf("cancel: %v", err)
			}
		})
	}
	for _, tc := range []struct {
		body  string
		names []string
		fail  bool
	}{
		{`{}`, nil, false}, {`{"data":null}`, nil, false}, {`{"data":[{}]}`, nil, false},
		{`{"data":[{"id":" x"}]}`, []string{"x"}, false},
		{`{"data":[{"id":"x"},{"id":"x"}]}`, []string{"x", "x"}, false},
		{`{"data":[],"error":{"message":"failed"}}`, nil, true},
		{`{"data":[]} {}`, nil, true},
	} {
		t.Run(tc.body, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, tc.body) }))
			defer srv.Close()
			d := NewZhipuAIModel(map[string]string{"default": srv.URL}, URLSuffix{Models: "models"})
			got, err := d.ListModels(&APIConfig{ApiKey: stringPtr("key")})
			if (err != nil) != tc.fail || joinModelNames(got, "|") != strings.Join(tc.names, "|") {
				t.Fatal("canonical typed discovery", got, err)
			}
		})
	}
}

func TestProviderDiscoveryRejectsHTTPErrorAndOversizedJSON(t *testing.T) {
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		w.Write([]byte("private provider error"))
	}))
	defer srv.Close()
	d := NewZhipuAIModel(map[string]string{"default": srv.URL}, URLSuffix{Models: "models"})
	if _, err := d.ListModels(&APIConfig{ApiKey: stringPtr("key")}); err == nil || !strings.Contains(err.Error(), "401") || strings.Contains(err.Error(), "private provider error") {
		t.Fatal("HTTP error contract", err)
	}
	oversized := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"data":[]}`+strings.Repeat(" ", modelResponseLimitForTest)+`{}`)
	}))
	if _, err := NewZhipuAIModel(map[string]string{"default": oversized.URL}, URLSuffix{Models: "models"}).ListModels(&APIConfig{ApiKey: stringPtr("key")}); err == nil {
		t.Fatal("oversized trailing JSON accepted")
	}
}
