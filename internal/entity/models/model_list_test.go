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
		if r.Method != http.MethodGet || r.URL.Path != "/models" {
			t.Errorf("request = %s %s", r.Method, r.URL.Path)
		}
		fmt.Fprint(w, `{"data":[{"id":"moonshot-v1-auto"},{"id":"kimi-k2.5"}]}`)
	}))
	defer server.Close()
	apiKey := "key"
	model := NewMoonshotModel(map[string]string{"default": server.URL}, URLSuffix{Models: "models"})

	models, err := model.ListModels(&APIConfig{APIKey: &apiKey})
	if err != nil {
		t.Fatalf("ListModels() error = %v", err)
	}
	if !reflect.DeepEqual(models, []string{"moonshot-v1-auto", "kimi-k2.5"}) {
		t.Fatalf("models = %#v", models)
	}
}

func TestMoonshotBalance(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/users/me/balance" {
			t.Errorf("request = %s %s", r.Method, r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer key" {
			t.Errorf("Authorization = %q", got)
		}
		fmt.Fprint(w, `{"code":0,"data":{"available_balance":49.58894,"voucher_balance":46.58893,"cash_balance":3.00001},"status":true}`)
	}))
	defer server.Close()
	apiKey := "key"
	model := NewMoonshotModel(map[string]string{"default": server.URL}, URLSuffix{Balance: "users/me/balance"})

	balance, err := model.Balance(&APIConfig{APIKey: &apiKey})
	if err != nil {
		t.Fatalf("Balance() error = %v", err)
	}
	want := map[string]interface{}{"balance": 49.58894, "currency": "CNY"}
	if !reflect.DeepEqual(balance, want) {
		t.Fatalf("balance = %#v", balance)
	}
}

func TestMoonshotBalanceMissingField(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"code":0,"data":{},"status":true}`)
	}))
	defer server.Close()
	apiKey := "key"
	model := NewMoonshotModel(map[string]string{"default": server.URL}, URLSuffix{Balance: "users/me/balance"})

	if _, err := model.Balance(&APIConfig{APIKey: &apiKey}); err == nil {
		t.Fatal("Balance() expected error for missing available_balance")
	}
}
