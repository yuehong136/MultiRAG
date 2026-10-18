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

func TestDeepSeekBalancePreflightAndCancel(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
	defer server.Close()
	driver := NewDeepSeekModel(map[string]string{"default": server.URL}, URLSuffix{Balance: "user/balance"})
	key, empty, unknown := "fixture", "", "missing"
	for _, api := range []*APIConfig{nil, {}, {APIKey: &empty}, {APIKey: &key, Region: &unknown}} {
		if got, err := driver.Balance(api); err == nil || got != nil {
			t.Fatal(got, err)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if got, err := driver.Balance(&APIConfig{APIKey: &key, Context: ctx}); !errors.Is(err, context.Canceled) || got != nil {
		t.Fatal(got, err)
	}
	if calls.Load() != 0 {
		t.Fatal("invalid/cancelled request reached provider")
	}
	testBalanceCancelDuringHTTP(t, func(baseURL string) ModelDriver {
		return NewDeepSeekModel(map[string]string{"default": baseURL}, URLSuffix{Balance: "user/balance"})
	})
}

func TestDeepSeekBalanceCurrenciesAndAvailabilityHTTP(t *testing.T) {
	for _, available := range []bool{true, false} {
		t.Run(fmt.Sprint(available), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet || r.URL.Path != "/user/balance" || r.Header.Get("Authorization") != "Bearer fixture" || r.ContentLength != 0 {
					t.Errorf("bad balance request: %s %s", r.Method, r.URL.Path)
				}
				fmt.Fprintf(w, `{"is_available":%t,"balance_infos":[{"currency":"USD","total_balance":"0","granted_balance":"0","topped_up_balance":"0"},{"currency":"CNY","total_balance":"20.5","granted_balance":"2","topped_up_balance":"18.5"}]}`, available)
			}))
			defer server.Close()
			driver := NewDeepSeekModel(map[string]string{"default": server.URL + "/"}, URLSuffix{Balance: "/user/balance"})
			key, region := "fixture", ""
			got, err := driver.Balance(&APIConfig{APIKey: &key, Region: &region})
			want := map[string]interface{}{
				"balance": float64(0), "currency": "USD", "is_available": available,
				"balance_infos": []map[string]interface{}{
					{"currency": "USD", "total_balance": float64(0), "granted_balance": float64(0), "topped_up_balance": float64(0)},
					{"currency": "CNY", "total_balance": 20.5, "granted_balance": float64(2), "topped_up_balance": 18.5},
				},
			}
			if err != nil || !reflect.DeepEqual(got, want) {
				t.Fatal(got, err)
			}
		})
	}
}

func TestDeepSeekBalanceRejectsInvalidResponsesHTTP(t *testing.T) {
	for _, tc := range []struct {
		name, body string
		status     int
	}{
		{"HTTP error", "private-body", 503},
		{"business error", `{"error":{"message":"private-body"},"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":"1"}]}`, 200},
		{"missing availability", `{"balance_infos":[{"currency":"CNY","total_balance":"1"}]}`, 200},
		{"missing balances", `{"is_available":true}`, 200},
		{"null balances", `{"is_available":true,"balance_infos":null}`, 200},
		{"empty balances", `{"is_available":false,"balance_infos":[]}`, 200},
		{"missing currency", `{"is_available":true,"balance_infos":[{"total_balance":"1"}]}`, 200},
		{"missing total", `{"is_available":true,"balance_infos":[{"currency":"CNY"}]}`, 200},
		{"wrong total type", `{"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":1}]}`, 200},
		{"invalid total", `{"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":"private-body"}]}`, 200},
		{"nonfinite total", `{"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":"NaN"}]}`, 200},
		{"second invalid", `{"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":"1"},{"currency":"USD","total_balance":"+Inf"}]}`, 200},
		{"invalid grant", `{"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":"1","granted_balance":"NaN"}]}`, 200},
		{"invalid topup", `{"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":"1","topped_up_balance":"1e999"}]}`, 200},
		{"malformed", `{`, 200}, {"null", `null`, 200},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(tc.status); fmt.Fprint(w, tc.body) }))
			defer server.Close()
			key := "fixture"
			driver := NewDeepSeekModel(map[string]string{"default": server.URL}, URLSuffix{Balance: "user/balance"})
			got, err := driver.Balance(&APIConfig{APIKey: &key})
			if err == nil || got != nil || strings.Contains(err.Error(), "private-body") {
				t.Fatal(got, err)
			}
		})
	}
}
