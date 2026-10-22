package models

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
)

func TestVolcEngineDiscoveryInventory(t *testing.T) {
	for _, test := range []struct {
		name, body string
		status     int
		want       []string
		wantErr    bool
	}{
		{name: "ordered IDs", body: `{"data":[{"id":"doubao-b","owned_by":"bytedance"},{"id":"doubao-a"}]}`, want: []string{"doubao-b", "doubao-a"}},
		{name: "empty inventory", body: `{"data":[]}`, want: []string{}},
		{name: "missing data", body: `{}`, wantErr: true},
		{name: "null data", body: `{"data":null}`, wantErr: true},
		{name: "wrong envelope", body: `{"Models":[{"id":"doubao"}]}`, wantErr: true},
		{name: "missing ID", body: `{"data":[{}]}`, wantErr: true},
		{name: "blank ID", body: `{"data":[{"id":" "}]}`, wantErr: true},
		{name: "padded ID", body: `{"data":[{"id":" doubao"}]}`, wantErr: true},
		{name: "duplicate ID", body: `{"data":[{"id":"doubao"},{"id":"doubao"}]}`, wantErr: true},
		{name: "invalid ID type", body: `{"data":[{"id":1}]}`, wantErr: true},
		{name: "business error", body: `{"data":[],"error":{"message":"private provider failure"}}`, wantErr: true},
		{name: "malformed JSON", body: `{"data":`, wantErr: true},
		{name: "trailing JSON", body: `{"data":[]} {}`, wantErr: true},
		{name: "HTTP failure", body: `private provider failure`, status: http.StatusUnauthorized, wantErr: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet || r.URL.Path != "/api/v3/models" || r.Header.Get("Authorization") != "Bearer key" {
					t.Errorf("unexpected discovery request: %s %s", r.Method, r.URL.Path)
				}
				if test.status != 0 {
					w.WriteHeader(test.status)
				}
				fmt.Fprint(w, test.body)
			}))
			defer server.Close()
			driver := NewVolcEngine(map[string]string{"default": server.URL + "/api/v3/"}, URLSuffix{Models: "models"})
			key := `{"ark_api_key":"key","endpoint_id":"ep"}`
			names, err := driver.ListModels(&APIConfig{APIKey: &key})
			if (err != nil) != test.wantErr || !reflect.DeepEqual(names, test.want) {
				t.Fatalf("ListModels() = %v, %v; want %v, error=%v", names, err, test.want, test.wantErr)
			}
			if err != nil && strings.Contains(err.Error(), "private provider failure") {
				t.Fatal("provider body leaked through discovery error")
			}
		})
	}
}

func TestVolcEngineDiscoveryEndpointValidation(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/api/v3/models" {
			t.Errorf("path = %q", r.URL.Path)
		}
		fmt.Fprint(w, `{"data":[]}`)
	}))
	defer server.Close()
	key, empty, unknown := "key", "", "unknown"
	for _, region := range []*string{nil, &empty} {
		driver := NewVolcEngine(map[string]string{"default": server.URL + "/api/v3/"}, URLSuffix{Models: " /models/ "})
		if _, err := driver.ListModels(&APIConfig{APIKey: &key, Region: region}); err != nil {
			t.Fatal(err)
		}
	}
	before := calls.Load()
	for _, test := range []struct {
		name   string
		urls   map[string]string
		suffix string
		config *APIConfig
	}{
		{"nil config", map[string]string{"default": server.URL}, "models", nil},
		{"missing key", map[string]string{"default": server.URL}, "models", &APIConfig{}},
		{"unknown region", map[string]string{"default": server.URL}, "models", &APIConfig{APIKey: &key, Region: &unknown}},
		{"missing default", nil, "models", &APIConfig{APIKey: &key}},
		{"missing suffix", map[string]string{"default": server.URL}, "", &APIConfig{APIKey: &key}},
		{"blank suffix", map[string]string{"default": server.URL}, " / ", &APIConfig{APIKey: &key}},
	} {
		t.Run(test.name, func(t *testing.T) {
			driver := NewVolcEngine(test.urls, URLSuffix{Models: test.suffix})
			if names, err := driver.ListModels(test.config); err == nil || names != nil {
				t.Fatalf("ListModels() = %v, %v", names, err)
			}
		})
	}
	if calls.Load() != before {
		t.Fatal("invalid configuration reached HTTP")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	driver := NewVolcEngine(map[string]string{"default": server.URL}, URLSuffix{Models: "models"})
	if _, err := driver.ListModels(&APIConfig{APIKey: &key, Context: ctx}); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancellation error = %v", err)
	}
}
