package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

func TestAliyunRegionResolutionAcrossOperations(t *testing.T) {
	var calls atomic.Int32
	var lastPath atomic.Value
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		lastPath.Store(r.URL.Path)
		if r.Header.Get("Authorization") != "Bearer key" {
			t.Error("wrong credential")
		}
		if r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/models") {
			fmt.Fprint(w, `{"data":[{"id":"qwen"}]}`)
			return
		}
		if r.Method != http.MethodPost || !strings.HasSuffix(r.URL.Path, "/chat/completions") {
			t.Errorf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		var body struct {
			Stream bool `json:"stream"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body.Stream {
			fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
			return
		}
		fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
	}))
	defer server.Close()
	model := NewAliyunModel(map[string]string{"default": server.URL + "/default/", "singapore": server.URL + "/singapore/", "empty": ""}, URLSuffix{Chat: "/chat/completions", Models: "/models"})
	if model.Name() != "aliyun" {
		t.Fatalf("Name() = %q", model.Name())
	}
	key, empty, singapore, unknown, emptyURL := "key", "", "singapore", "unknown", "empty"
	for name, operation := range map[string]func(*APIConfig) error{
		"chat": func(config *APIConfig) error {
			_, err := model.ChatWithMessages("qwen", config, []Message{{Role: "user", Content: "q"}}, nil)
			return err
		},
		"stream": func(config *APIConfig) error {
			return model.ChatStreamlyWithMessages("qwen", []Message{{Role: "user", Content: "q"}}, config, nil, func(*string, *string) error { return nil })
		},
		"list":  func(config *APIConfig) error { _, err := model.ListModels(config); return err },
		"check": model.CheckConnection,
	} {
		t.Run(name, func(t *testing.T) {
			for _, region := range []*string{nil, &empty, &singapore} {
				before := calls.Load()
				config := &APIConfig{APIKey: &key, Region: region}
				endpoint, _, err := model.endpoint(config, "models")
				prefix := "/default/"
				if region == &singapore {
					prefix = "/singapore/"
				}
				if err != nil || endpoint != server.URL+prefix+"models" {
					t.Fatalf("endpoint = %q, %v", endpoint, err)
				}
				if err := operation(config); err != nil || calls.Load() != before+1 {
					t.Fatalf("operation error=%v calls=%d", err, calls.Load()-before)
				}
				if path := lastPath.Load().(string); !strings.HasPrefix(path, prefix) {
					t.Fatalf("operation used region path %q, want prefix %q", path, prefix)
				}
			}
			for _, config := range []*APIConfig{nil, {}, {APIKey: &empty}, {APIKey: &key, Region: &unknown}, {APIKey: &key, Region: &emptyURL}} {
				before := calls.Load()
				if err := operation(config); err == nil || !strings.Contains(err.Error(), "aliyun:") {
					t.Fatalf("invalid config error = %v", err)
				}
				if calls.Load() != before {
					t.Fatal("invalid configuration issued HTTP request")
				}
			}
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			if err := operation(&APIConfig{APIKey: &key, Context: ctx}); !errors.Is(err, context.Canceled) {
				t.Fatalf("cancellation = %v", err)
			}
		})
	}
	missing := NewAliyunModel(nil, URLSuffix{Models: "models"})
	if _, err := missing.ListModels(&APIConfig{APIKey: &key}); err == nil || !strings.Contains(err.Error(), "no base URL") {
		t.Fatalf("missing default error = %v", err)
	}
}
