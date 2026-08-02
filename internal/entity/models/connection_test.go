package models

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestMinimaxCheckConnectionUsesConfiguredRegionAndBearerToken(t *testing.T) {
	var gotPath string
	var gotAuthorization string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotAuthorization = r.Header.Get("Authorization")
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	model := NewMinimaxModel(
		map[string]string{"default": "https://unused.invalid", "global": server.URL},
		URLSuffix{Files: "v1/files/list"},
	)
	apiKey := "secret"
	region := "global"
	if err := model.CheckConnection(&APIConfig{APIKey: &apiKey, Region: &region}); err != nil {
		t.Fatalf("CheckConnection() error = %v", err)
	}
	if gotPath != "/v1/files/list" {
		t.Fatalf("path = %q", gotPath)
	}
	if gotAuthorization != "Bearer secret" {
		t.Fatalf("Authorization = %q", gotAuthorization)
	}
}

func TestMinimaxCheckConnectionSurfacesProviderError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "invalid key", http.StatusUnauthorized)
	}))
	defer server.Close()

	model := NewMinimaxModel(map[string]string{"default": server.URL}, URLSuffix{Files: "files"})
	apiKey := "bad"
	err := model.CheckConnection(&APIConfig{APIKey: &apiKey})
	if err == nil || !strings.Contains(err.Error(), "invalid key") {
		t.Fatalf("CheckConnection() error = %v", err)
	}
}

func TestMinimaxCheckConnectionRejectsMissingAPIKey(t *testing.T) {
	model := NewMinimaxModel(map[string]string{"default": "https://unused.invalid"}, URLSuffix{Files: "files"})
	if err := model.CheckConnection(nil); err == nil {
		t.Fatal("CheckConnection() error = nil, want missing API key error")
	}
}
