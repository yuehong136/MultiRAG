package models

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestGiteeBalanceRawContract(t *testing.T) {
	for _, test := range []struct {
		body string
		want float64
		ok   bool
	}{
		{`{"balance":0}`, 0, true}, {`{"balance":-1.25,"code":0}`, -1.25, true},
		{`{"balance":12.5,"error":null}`, 12.5, true},
		{`{}`, 0, false}, {`null`, 0, false}, {`{"balance":null}`, 0, false},
		{`{"balance":"0"}`, 0, false}, {`{"balance":true}`, 0, false}, {`{"balance":[]}`, 0, false},
		{`{"balance":1e309}`, 0, false}, {`{"balance":NaN}`, 0, false},
		{`{"balance":0,"error":"private-provider-body"}`, 0, false},
		{`{"balance":0,"code":1}`, 0, false}, {`{"balance":0,"code":null}`, 0, false},
		{`{"balance":0} {}`, 0, false}, {`{"balance":0} trailing`, 0, false},
	} {
		t.Run(test.body, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				raw, _ := io.ReadAll(r.Body)
				if r.Method != "GET" || r.URL.Path != "/instance/balance" || len(raw) != 0 || r.Header.Get("Accept") != "application/json" || r.Header.Get("Authorization") != "Bearer key" {
					t.Error("incorrect balance request")
				}
				fmt.Fprint(w, test.body)
			}))
			base := map[string]string{"default": "http://catalog.invalid", "office": srv.URL + "/instance/"}
			d := NewGiteeModel(base, URLSuffix{Balance: "/balance"})
			base["office"] = "http://changed.invalid"
			api := &APIConfig{APIKey: stringPtr(" key "), Region: stringPtr("office")}
			result, err := d.Balance(api)
			if test.ok {
				if err != nil || !reflect.DeepEqual(result, map[string]any{"balance": test.want, "currency": "CNY"}) {
					t.Fatal(result, err)
				}
			} else if err == nil || result != nil || strings.Contains(err.Error(), "private-provider-body") {
				t.Fatal(result, err)
			}
			if *api.APIKey != " key " || *api.Region != "office" {
				t.Fatal("request config mutated")
			}
		})
	}
}

func TestGiteePublicDiscoveryStrictIDs(t *testing.T) {
	for _, raw := range []string{
		`{"data":[{"id":"model","owned_by":"metadata"}]}`, `{"data":[]}`,
		`{}`, `{"data":null}`, `{"data":[{}]}`, `{"data":[{"id":3}]}`,
		`{"data":[{"id":" x"}]}`, `{"data":[{"id":"x"},{"id":"x"}]}`,
		`{"data":[],"error":{}}`, `{"data":[]} {}`,
	} {
		t.Run(raw, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				body, _ := io.ReadAll(r.Body)
				if r.Method != "GET" || r.URL.Path != "/models" || len(body) != 0 || r.Header.Get("Authorization") != "" {
					t.Error("public discovery sent credentials or GET body")
				}
				fmt.Fprint(w, raw)
			}))
			d := NewGiteeModel(map[string]string{"default": srv.URL}, URLSuffix{Models: "models"})
			for _, api := range []*APIConfig{nil, {APIKey: stringPtr("private-key")}} {
				got, err := d.ListModels(api)
				valid := raw == `{"data":[]}` || strings.Contains(raw, "metadata")
				if valid && (err != nil || got == nil) || !valid && (err == nil || got != nil) {
					t.Fatal(got, err)
				}
				if strings.Contains(raw, "metadata") && !reflect.DeepEqual(got, []string{"model"}) {
					t.Fatal("discovery altered raw ID", got)
				}
			}
		})
	}
}

func TestGiteeMetadataPreflightAndSafeErrors(t *testing.T) {
	var calls atomic.Int32
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusUnauthorized)
		fmt.Fprint(w, "private-provider-body")
	}))
	d := NewGiteeModel(map[string]string{"default": srv.URL}, URLSuffix{Balance: "balance", Models: "models"})
	for _, api := range []*APIConfig{nil, {}, {APIKey: stringPtr(" ")}} {
		if result, err := d.Balance(api); err == nil || result != nil {
			t.Fatal("invalid credentials accepted")
		}
	}
	api := &APIConfig{APIKey: stringPtr("private-key"), Region: stringPtr("missing")}
	if _, err := d.Balance(api); err == nil {
		t.Fatal("unknown region accepted")
	}
	if _, err := d.ListModels(api); err == nil {
		t.Fatal("unknown discovery region accepted")
	}
	if calls.Load() != 0 {
		t.Fatal("invalid request reached provider")
	}
	for _, balance := range []bool{false, true} {
		var err error
		if balance {
			_, err = d.Balance(&APIConfig{APIKey: stringPtr("private-key")})
		} else {
			_, err = d.ListModels(nil)
		}
		if err == nil || !strings.Contains(err.Error(), "401") || strings.Contains(err.Error(), srv.URL) || strings.Contains(err.Error(), "private-") {
			t.Fatal("unsafe or missing status error", err)
		}
	}
	invalid := NewGiteeModel(map[string]string{"default": "http://user:private-key@%bad/private-path"}, URLSuffix{Balance: "balance", Models: "models"})
	if _, err := invalid.Balance(&APIConfig{APIKey: stringPtr("private-key")}); err == nil || strings.Contains(err.Error(), "private-") {
		t.Fatal("unsafe endpoint error", err)
	}
}

func TestGiteeMetadataBoundedAndCancelled(t *testing.T) {
	for _, balance := range []bool{false, true} {
		t.Run(fmt.Sprint(balance), func(t *testing.T) {
			for _, oversized := range []bool{false, true} {
				started, stopped := make(chan struct{}), make(chan struct{})
				srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					close(started)
					if oversized {
						io.Copy(w, io.LimitReader(strings.NewReader(strings.Repeat(" ", MaxMediaBytes+1)), MaxMediaBytes+1))
					} else {
						w.WriteHeader(200)
						w.(http.Flusher).Flush()
						<-r.Context().Done()
					}
					close(stopped)
				}))
				d := NewGiteeModel(map[string]string{"default": srv.URL}, URLSuffix{Balance: "balance", Models: "models"})
				ctx, cancel := context.WithCancel(t.Context())
				defer cancel()
				done := make(chan error, 1)
				go func() {
					api := &APIConfig{Context: ctx, APIKey: stringPtr("key")}
					var err error
					if balance {
						_, err = d.Balance(api)
					} else {
						_, err = d.ListModels(api)
					}
					done <- err
				}()
				select {
				case <-started:
				case <-time.After(time.Second):
					t.Fatal("request did not start")
				}
				if !oversized {
					cancel()
				}
				select {
				case err := <-done:
					if err == nil || !oversized && !errors.Is(err, context.Canceled) {
						t.Fatal(err)
					}
				case <-time.After(3 * time.Second):
					t.Fatal("response read did not stop")
				}
				cancel()
				select {
				case <-stopped:
				case <-time.After(time.Second):
					t.Fatal("request leaked")
				}
			}
		})
	}
}

func TestGiteeConcurrentBalanceInstances(t *testing.T) {
	var wg sync.WaitGroup
	for _, amount := range []int{1, 2} {
		key := fmt.Sprint(amount)
		srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Header.Get("Authorization") != "Bearer "+key {
				t.Error("account credentials crossed")
			}
			fmt.Fprintf(w, `{"balance":%d}`, amount)
		}))
		d := NewGiteeModel(map[string]string{"default": srv.URL}, URLSuffix{Balance: "balance"})
		for range 8 {
			wg.Add(1)
			go func() {
				defer wg.Done()
				result, err := d.Balance(&APIConfig{APIKey: &key})
				if err != nil || result["balance"] != float64(amount) {
					t.Error(result, err)
				}
			}()
		}
	}
	wg.Wait()
}

func TestGiteeMetadataDoesNotFollowRedirects(t *testing.T) {
	var forwarded atomic.Int32
	target := providerTestServer(t, http.HandlerFunc(func(http.ResponseWriter, *http.Request) { forwarded.Add(1) }))
	origin := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL+"/private-target", http.StatusFound)
	}))
	d := NewGiteeModel(map[string]string{"default": origin.URL}, URLSuffix{Balance: "balance", Models: "models"})
	if _, err := d.Balance(&APIConfig{APIKey: stringPtr("private-key")}); err == nil || strings.Contains(err.Error(), origin.URL) || strings.Contains(err.Error(), "private-") {
		t.Fatal("unsafe redirect error", err)
	}
	if _, err := d.ListModels(nil); err == nil {
		t.Fatal("discovery followed redirect")
	}
	if forwarded.Load() != 0 || d.httpClient.CheckRedirect != nil {
		t.Fatal("redirect followed or shared client mutated")
	}
}
