package models

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
)

func TestHunyuanDiscoveryRawIDsAndFailures(t *testing.T) {
	for _, tc := range []struct {
		body  string
		valid bool
		want  []string
	}{
		{`{"data":[{"id":"hunyuan-standard-256K","owned_by":"vendor"},{"id":"hunyuan-pro"},{"id":"Hunyuan-Custom"}]}`, true, []string{"hunyuan-standard-256K", "hunyuan-pro", "Hunyuan-Custom"}},
		{`{"data":[]}`, true, []string{}},
		{`{}`, false, nil},
		{`{"data":null}`, false, nil},
		{`{"data":[{"id":17}]}`, false, nil},
		{`{"data":[{"id":" padded"}]}`, false, nil},
		{`{"data":[{"id":"same"},{"id":"same"}]}`, false, nil},
		{`{"data":[],"error":{"message":"private-body"}}`, false, nil},
		{`{"data":[]} {}`, false, nil},
	} {
		t.Run(tc.body, func(t *testing.T) {
			server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet || r.URL.Path != "/v1/models" || r.ContentLength > 0 || r.Header.Get("Authorization") != "Bearer key" {
					t.Error("discovery changed the configured endpoint, bodyless GET or authentication")
				}
				fmt.Fprint(w, tc.body)
			}))
			driver := NewHunyuanModel(map[string]string{"default": server.URL + "/v1"}, URLSuffix{Models: "models"})
			api := &APIConfig{APIKey: stringPtr(" key ")}
			got, err := driver.ListModels(api)
			if tc.valid && (err != nil || !reflect.DeepEqual(got, tc.want)) || !tc.valid && (err == nil || got != nil || strings.Contains(err.Error(), "private-body")) {
				t.Fatalf("discovery=%v error=%v", got, err)
			}
			if *api.APIKey != " key " {
				t.Fatal("caller credentials mutated")
			}
		})
	}
}

func TestHunyuanDiscoveryPreflightHasNoHTTP(t *testing.T) {
	var calls atomic.Int32
	server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		fmt.Fprint(w, `{"data":[]}`)
	}))
	driver := NewHunyuanModel(map[string]string{"default": server.URL}, URLSuffix{Models: "models"})
	for _, api := range []*APIConfig{nil, {}, {APIKey: stringPtr(" ")}, {APIKey: stringPtr("key"), Region: stringPtr("unknown")}} {
		if rows, err := driver.ListModels(api); err == nil || rows != nil {
			t.Fatal("invalid discovery configuration accepted")
		}
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if rows, err := driver.ListModels(&APIConfig{APIKey: stringPtr("key"), Context: ctx}); !errors.Is(err, context.Canceled) || rows != nil {
		t.Fatal("cancelled discovery accepted", rows, err)
	}
	if calls.Load() != 0 {
		t.Fatal("rejected discovery reached HTTP")
	}
}
