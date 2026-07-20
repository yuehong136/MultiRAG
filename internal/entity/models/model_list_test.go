package models

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

func TestDeepSeekListModels(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/models" {
			t.Errorf("request = %s %s", r.Method, r.URL.Path)
		}
		fmt.Fprint(w, `{"data":[{"id":"deepseek-chat"},{"id":"deepseek-reasoner"}]}`)
	}))
	defer server.Close()
	apiKey := "key"
	model := NewDeepSeekModel(map[string]string{"default": server.URL}, URLSuffix{Models: "models"})

	models, err := model.ListModels(&APIConfig{APIKey: &apiKey})
	if err != nil {
		t.Fatalf("ListModels() error = %v", err)
	}
	if !reflect.DeepEqual(models, []string{"deepseek-chat", "deepseek-reasoner"}) {
		t.Fatalf("models = %#v", models)
	}
}

func TestMoonshotListModels(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/models" {
			t.Errorf("request = %s %s", r.Method, r.URL.Path)
		}
		fmt.Fprint(w, `{"models":["moonshot-v1-auto"]}`)
	}))
	defer server.Close()
	apiKey := "key"
	model := NewMoonshotModel(map[string]string{"default": server.URL}, URLSuffix{Models: "models"})

	models, err := model.ListModels(&APIConfig{APIKey: &apiKey})
	if err != nil {
		t.Fatalf("ListModels() error = %v", err)
	}
	if !reflect.DeepEqual(models, []string{"moonshot-v1-auto"}) {
		t.Fatalf("models = %#v", models)
	}
}
