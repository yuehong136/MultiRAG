package models

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
)

func TestCometAPIEmbeddingsHTTP(t *testing.T) { testHostedEmbeddingsHTTP(t, "CometAPI") }

func TestCometAPIQuotaHTTP(t *testing.T) {
	for _, body := range []string{`{"total_quota":0}`, `{"total_quota":20.5,"total_used_quota":1.25,"request_count":7}`, `{}`, `null`, `{"total_quota":null}`, `{"total_quota":-1}`, `{"total_quota":"0"}`, `{"total_quota":1e999}`, `{"total_quota":0,"request_count":0.5}`, `{"total_quota":0,"total_used_quota":null}`, `{"total_quota":0,"error":{"message":"private-body"}}`, `{"total_quota":0,"success":false}`, `{"total_quota":0} {}`} {
		t.Run(body, func(t *testing.T) {
			quota := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != "GET" || r.URL.Path != "/user/quota" || r.URL.Query().Get("key") != "private-key +?" || r.URL.Query().Get("keep") != "1" || r.Header.Get("Authorization") != "" {
					t.Error("quota transport lost contract")
				}
				fmt.Fprint(w, body)
			}))
			defer quota.Close()
			d := NewCometAPIModel(map[string]string{"default": "http://chat.invalid"}, URLSuffix{Balance: quota.URL + "/user/quota?keep=1"})
			api := &APIConfig{APIKey: stringPtr("private-key +?")}
			result, err := d.Balance(api)
			valid := body == `{"total_quota":0}` || body == `{"total_quota":20.5,"total_used_quota":1.25,"request_count":7}`
			if valid {
				if err != nil || result == nil {
					t.Fatal(result, err)
				}
			} else if err == nil || result != nil || strings.Contains(err.Error(), "private-") {
				t.Fatal(result, err)
			}
			if err := d.CheckConnection(api); (err == nil) != valid {
				t.Fatal(err)
			}
		})
	}
}

func TestCometAPIQuotaFailureRedaction(t *testing.T) {
	forwarded := 0
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { forwarded++ }))
	defer target.Close()
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL+"/stolen?key=private-key", http.StatusTemporaryRedirect)
	}))
	defer redirect.Close()
	d := NewCometAPIModel(nil, URLSuffix{Balance: redirect.URL})
	if result, err := d.Balance(&APIConfig{APIKey: stringPtr("private-key")}); err == nil || result != nil || strings.Contains(err.Error(), "private-key") || forwarded != 0 {
		t.Fatal(result, err, forwarded)
	}
	d.httpClient.Transport = lmStudioTransportFunc(func(r *http.Request) (*http.Response, error) { return nil, fmt.Errorf("transport private-key") })
	if result, err := d.Balance(&APIConfig{APIKey: stringPtr("private-key")}); err == nil || result != nil || strings.Contains(err.Error(), "private-key") {
		t.Fatal(result, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	d = NewCometAPIModel(nil, URLSuffix{Balance: redirect.URL})
	if result, err := d.Balance(&APIConfig{APIKey: stringPtr("private-key"), Context: ctx}); !errors.Is(err, context.Canceled) || result != nil {
		t.Fatal(result, err)
	}
	for _, suffix := range []string{"", "https://user:secret@example.com/quota", "http://["} {
		d = NewCometAPIModel(nil, URLSuffix{Balance: suffix})
		if result, err := d.Balance(&APIConfig{APIKey: stringPtr("private-key")}); err == nil || result != nil || strings.Contains(err.Error(), "private-key") {
			t.Fatal(result, err)
		}
	}
}

func TestCometAPIDiscoveryHTTP(t *testing.T) {
	for _, body := range []string{`{"data":[]}`, `{"data":[{"id":"model"}]}`, `{}`, `{"data":null}`, `{"data":[{"id":""}]}`, `{"data":[{"id":"a"},{"id":"a"}]}`, `{"data":[]} {}`, `{"error":{"message":"private-body"}}`} {
		t.Run(body, func(t *testing.T) {
			fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/api/models" || r.Header.Get("Authorization") != "" {
					t.Error("public model discovery")
				}
				fmt.Fprint(w, body)
			}))
			defer fixture.Close()
			d := NewCometAPIModel(map[string]string{"default": fixture.URL}, URLSuffix{Models: "api/models"})
			result, err := d.ListModels(&APIConfig{APIKey: stringPtr("private-key")})
			valid := body == `{"data":[]}` || body == `{"data":[{"id":"model"}]}`
			if valid {
				if err != nil || result == nil {
					t.Fatal(result, err)
				}
				if len(result) == 1 && !reflect.DeepEqual(result, []string{"model"}) {
					t.Fatal(result)
				}
			} else if err == nil || result != nil {
				t.Fatal(result, err)
			}
		})
	}
}
