package models

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestSiliconFlowBalanceHTTP(t *testing.T) {
	for _, tc := range []struct {
		name, data string
		want       float64
	}{
		{"total preferred", `"totalBalance":"12.75","balance":"8"`, 12.75},
		{"balance fallback", `"balance":"3.5"`, 3.5},
		{"zero", `"totalBalance":"0","balance":"8"`, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet || r.URL.Path != "/office/v1/user/info" || r.Header.Get("Authorization") != "Bearer fixture" || r.ContentLength != 0 {
					t.Errorf("incorrect balance request: %s %s", r.Method, r.URL.Path)
				}
				fmt.Fprintf(w, `{"code":0,"status":true,"data":{%s}}`, tc.data)
			}))
			defer server.Close()
			driver := NewSiliconFlowModel(map[string]string{"default": "http://unreachable.invalid", "office": server.URL + "/office/v1/"}, URLSuffix{Balance: "/user/info"})
			key, region := "fixture", "office"
			api := &APIConfig{APIKey: &key, Region: &region}
			got, err := driver.Balance(api)
			if err != nil || !reflect.DeepEqual(got, map[string]interface{}{"balance": tc.want, "currency": "CNY"}) || api.Context != nil {
				t.Fatal(got, err)
			}
		})
	}
}

func TestSiliconFlowBalanceRejectsInvalidResponsesHTTP(t *testing.T) {
	for _, tc := range []struct {
		name, body string
		status     int
	}{
		{"business failure", `{"code":42,"status":false,"message":"private-body"}`, 200},
		{"contradictory code", `{"code":42,"status":true,"data":{"totalBalance":"1"}}`, 200},
		{"missing code", `{"status":true,"data":{"balance":"1"}}`, 200},
		{"missing status", `{"code":0,"data":{"balance":"1"}}`, 200},
		{"error envelope", `{"code":0,"status":true,"data":{"balance":"1"},"error":{"message":"private-body"}}`, 200},
		{"missing amount", `{"code":0,"status":true,"data":{}}`, 200},
		{"bad amount", `{"code":0,"status":true,"data":{"balance":"private-body"}}`, 200},
		{"wrong amount type", `{"code":0,"status":true,"data":{"balance":1}}`, 200},
		{"NaN", `{"code":0,"status":true,"data":{"balance":"NaN"}}`, 200},
		{"infinity", `{"code":0,"status":true,"data":{"balance":"+Inf"}}`, 200},
		{"overflow", `{"code":0,"status":true,"data":{"balance":"1e999"}}`, 200},
		{"malformed", `{"private-body":`, 200},
		{"null", `null`, 200},
		{"HTTP failure", "private-body", 401},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(tc.status); fmt.Fprint(w, tc.body) }))
			defer server.Close()
			key := "fixture"
			driver := NewSiliconFlowModel(map[string]string{"default": server.URL}, URLSuffix{Balance: "user/info"})
			got, err := driver.Balance(&APIConfig{APIKey: &key})
			if err == nil || got != nil || strings.Contains(err.Error(), "private-body") {
				t.Fatal(got, err)
			}
		})
	}
}

func TestSiliconFlowBalancePreflightAndCancel(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		fmt.Fprint(w, `{"code":0,"status":true,"data":{"balance":"1"}}`)
	}))
	defer server.Close()
	key, empty, unknown := "fixture", "", "missing"
	driver := NewSiliconFlowModel(map[string]string{"default": server.URL}, URLSuffix{Balance: "user/info"})
	for _, api := range []*APIConfig{nil, {}, {APIKey: &empty}, {APIKey: &key, Region: &unknown}} {
		if got, err := driver.Balance(api); err == nil || got != nil {
			t.Fatal(got, err)
		}
	}
	for _, d := range []*SiliconFlowModel{NewSiliconFlowModel(nil, URLSuffix{Balance: "user/info"}), NewSiliconFlowModel(map[string]string{"default": server.URL}, URLSuffix{})} {
		if _, err := d.Balance(&APIConfig{APIKey: &key}); err == nil {
			t.Fatal("missing endpoint accepted")
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
	if got, err := driver.Balance(&APIConfig{APIKey: &key, Region: &empty}); err != nil || got["balance"] != float64(1) || calls.Load() != 1 {
		t.Fatal(got, err)
	}
}

func testBalanceCancelDuringHTTP(t *testing.T, create func(string) ModelDriver) {
	t.Helper()
	started, stopped := make(chan struct{}), make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		r.Body.Close()
		w.WriteHeader(http.StatusOK)
		w.(http.Flusher).Flush()
		close(started)
		select {
		case <-r.Context().Done():
		case <-time.After(2 * time.Second):
			t.Error("balance request did not cancel")
		}
		close(stopped)
	}))
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go func() { <-started; cancel() }()
	key := "fixture"
	api := &APIConfig{APIKey: &key, Context: ctx}
	got, err := create(server.URL).Balance(api)
	if !errors.Is(err, context.Canceled) || got != nil || api.Context != ctx {
		t.Fatal(got, err)
	}
	select {
	case <-stopped:
	case <-time.After(time.Second):
		t.Fatal("balance HTTP cleanup failed")
	}
}

func TestSiliconFlowBalanceCancelDuringHTTP(t *testing.T) {
	testBalanceCancelDuringHTTP(t, func(baseURL string) ModelDriver {
		return NewSiliconFlowModel(map[string]string{"default": baseURL}, URLSuffix{Balance: "user/info"})
	})
}
