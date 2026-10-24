package models

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestNovitaBalanceNativeScaleAndRawFailures(t *testing.T) {
	for _, test := range []struct {
		body  string
		want  float64
		valid bool
	}{
		{`{"availableBalance":"0"}`, 0, true}, {`{"availableBalance":"12345"}`, 1.2345, true}, {`{"availableBalance":"-1"}`, -0.0001, true},
		{`{}`, 0, false}, {`{"availableBalance":null}`, 0, false}, {`{"availableBalance":0}`, 0, false}, {`{"availableBalance":""}`, 0, false}, {`{"availableBalance":" 1"}`, 0, false}, {`{"availableBalance":"1.5"}`, 0, false}, {`{"availableBalance":"NaN"}`, 0, false}, {`{"availableBalance":"Inf"}`, 0, false}, {`{"availableBalance":"1e309"}`, 0, false}, {`{"availableBalance":"0","error":"private-secret"}`, 0, false}, {`{"availableBalance":"0","code":1}`, 0, false}, {`{"availableBalance":"0"} {}`, 0, false},
	} {
		t.Run(test.body, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				raw, _ := io.ReadAll(r.Body)
				if r.Method != "GET" || len(raw) != 0 || r.Header.Get("Authorization") != "Bearer key" || r.URL.Path != "/custom/balance" {
					t.Error("wrong balance request")
				}
				fmt.Fprint(w, test.body)
			}))
			defer srv.Close()
			d := NewNovitaModel(map[string]string{"default": "http://invalid.invalid", "custom": srv.URL}, URLSuffix{Balance: "custom/balance"})
			result, err := d.Balance(&APIConfig{APIKey: stringPtr("key"), Region: stringPtr("custom")})
			if test.valid {
				if err != nil || result["balance"] != test.want || result["currency"] != "USD" {
					t.Fatal(result, err)
				}
			} else if err == nil || result != nil || strings.Contains(err.Error(), "private-secret") {
				t.Fatal(result, err)
			}
		})
	}
}

func TestMoonshotStrictGETResponses(t *testing.T) {
	for _, body := range []string{`{}`, `{"data":null}`, `{"data":[{}]}`, `{"data":[{"id":" x"}]}`, `{"data":[{"id":"x"},{"id":"x"}]}`, `{"data":[],"error":"private-secret"}`} {
		t.Run(body, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
			defer srv.Close()
			m := NewMoonshotModel(map[string]string{"default": srv.URL}, URLSuffix{Models: "models"})
			if result, err := m.ListModels(&APIConfig{APIKey: stringPtr("key")}); err == nil || result != nil {
				t.Fatal(result, err)
			}
		})
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" || r.ContentLength != 0 || r.Header.Get("Authorization") != "Bearer key" {
			t.Error("wrong GET")
		}
		if r.URL.Path == "/models" {
			fmt.Fprint(w, `{"data":[]}`)
		} else {
			fmt.Fprint(w, `{"data":{"available_balance":0}}`)
		}
	}))
	defer srv.Close()
	m := NewMoonshotModel(map[string]string{"default": srv.URL}, URLSuffix{Models: "models", Balance: "balance"})
	if result, err := m.ListModels(&APIConfig{APIKey: stringPtr("key"), Region: stringPtr("")}); err != nil || result == nil || len(result) != 0 {
		t.Fatal(result, err)
	}
	if result, err := m.Balance(&APIConfig{APIKey: stringPtr("key")}); err != nil || result["balance"] != float64(0) {
		t.Fatal(result, err)
	}
	if m.httpClient.Timeout != 0 {
		t.Fatal("healthy stream still has total deadline")
	}
}

func TestAfter440BalanceAndDiscoveryGETCancellation(t *testing.T) {
	for _, kind := range []string{"novita-balance", "moonshot-balance", "moonshot-models"} {
		t.Run(kind, func(t *testing.T) {
			started, closed := make(chan struct{}), make(chan struct{})
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { close(started); <-r.Context().Done(); close(closed) }))
			defer srv.Close()
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			done := make(chan error, 1)
			go func() {
				api := &APIConfig{APIKey: stringPtr("key"), Context: ctx}
				var err error
				switch kind {
				case "novita-balance":
					_, err = NewNovitaModel(map[string]string{"default": srv.URL}, URLSuffix{Balance: "balance"}).Balance(api)
				case "moonshot-balance":
					_, err = NewMoonshotModel(map[string]string{"default": srv.URL}, URLSuffix{Balance: "balance"}).Balance(api)
				case "moonshot-models":
					_, err = NewMoonshotModel(map[string]string{"default": srv.URL}, URLSuffix{Models: "models"}).ListModels(api)
				}
				done <- err
			}()
			select {
			case <-started:
			case <-time.After(time.Second):
				t.Fatal("GET did not start")
			}
			cancel()
			if err := <-done; !errors.Is(err, context.Canceled) {
				t.Fatal(err)
			}
			select {
			case <-closed:
			case <-time.After(time.Second):
				t.Fatal("GET leaked")
			}
		})
	}
}

func TestMoonshotActiveAndStalledStreamLifecycle(t *testing.T) {
	for _, active := range []bool{false, true} {
		t.Run(fmt.Sprint(active), func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if active {
					for range 10 {
						fmt.Fprint(w, ": heartbeat\n")
						w.(http.Flusher).Flush()
						time.Sleep(10 * time.Millisecond)
					}
					fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
					return
				}
				fmt.Fprint(w, "data:")
				w.(http.Flusher).Flush()
				<-r.Context().Done()
			}))
			defer srv.Close()
			m := NewMoonshotModel(map[string]string{"default": srv.URL}, URLSuffix{Chat: "chat"})
			m.streamIdleTimeout = 50 * time.Millisecond
			err := m.ChatStreamlyWithMessages("kimi", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: stringPtr("key")}, nil, func(*string, *string) error { return nil })
			if active && err != nil || !active && !errors.Is(err, context.DeadlineExceeded) {
				t.Fatal(err)
			}
		})
	}
}
