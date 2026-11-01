package models

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"reflect"
	"strings"
	"testing"
)

func TestSiliconFlowDiscoveryIdentityAndFailuresHTTP(t *testing.T) {
	for _, test := range []struct {
		name, body string
		want       []string
	}{
		{"owned", `{"data":[{"id":"org/raw","owned_by":"owner"},{"id":"plain"},{"id":"org/raw@revision","owned_by":"owner"}]}`, []string{"org/raw@owner", "plain", "org/raw@revision@owner"}},
		{"empty", `{"data":[]}`, []string{}},
		{"missing", `{}`, nil},
		{"null", `{"data":null}`, nil},
		{"null-item", `{"data":[null]}`, nil},
		{"bad-id", `{"data":[{"id":1}]}`, nil},
		{"empty-id", `{"data":[{"id":""}]}`, nil},
		{"whitespace-id", `{"data":[{"id":" raw "}]}`, nil},
		{"duplicate", `{"data":[{"id":"raw","owned_by":"one"},{"id":"raw","owned_by":"two"}]}`, nil},
		{"error", `{"data":[{"id":"raw"}],"error":"private provider message"}`, nil},
		{"trailing", `{"data":[]} {}`, nil},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet || r.URL.Path != "/instance/models" || r.Header.Get("Authorization") != "Bearer fixture" {
					t.Error("discovery instance or credentials changed")
				}
				fmt.Fprint(w, test.body)
			}))
			driver := NewSiliconFlowModel(map[string]string{"default": server.URL + "/instance"}, URLSuffix{Models: "models"})
			api := &APIConfig{APIKey: stringPtr("fixture")}
			got, err := driver.ListModels(api)
			if test.want == nil {
				if err == nil || got != nil || strings.Contains(err.Error(), "private provider message") {
					t.Fatalf("invalid discovery published %v, %v", got, err)
				}
			} else if err != nil || !reflect.DeepEqual(got, test.want) {
				t.Fatalf("discovery = %#v, %v; want %#v", got, err, test.want)
			}
			if api.Context != nil {
				t.Fatal("discovery mutated caller context")
			}
			providerResourceRecord(t, map[string]any{"action": "discovery-contract", "names": got, "valid": test.want != nil})
		})
	}
}

type siliconFlowDiscoveryBody struct {
	io.Reader
	read   int
	closed bool
	cancel context.CancelFunc
}

func (b *siliconFlowDiscoveryBody) Read(data []byte) (int, error) {
	n, err := b.Reader.Read(data)
	b.read += n
	if err == io.EOF && b.cancel != nil {
		b.cancel()
	}
	return n, err
}

func (b *siliconFlowDiscoveryBody) Close() error { b.closed = true; return nil }

func TestSiliconFlowDiscoveryReadBoundAndClose(t *testing.T) {
	for _, scenario := range []string{"oversized", "cancel", "success", "status"} {
		t.Run(scenario, func(t *testing.T) {
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			raw := `{"data":[{"id":"raw","owned_by":"owner"}]}`
			if scenario == "oversized" {
				raw += strings.Repeat(" ", int(MaxMediaBytes)+4096)
			}
			body := &siliconFlowDiscoveryBody{Reader: strings.NewReader(raw)}
			if scenario == "cancel" {
				body.cancel = cancel
			}
			driver := NewSiliconFlowModel(map[string]string{"default": "http://controlled.invalid"}, URLSuffix{Models: "models"})
			driver.httpClient.Transport = providerContractTransport(func(r *http.Request) (*http.Response, error) {
				status := http.StatusOK
				if scenario == "status" {
					status = http.StatusUnauthorized
				}
				return &http.Response{StatusCode: status, Body: body}, nil
			})
			names, err := driver.ListModels(&APIConfig{APIKey: stringPtr("fixture"), Context: ctx})
			if !body.closed || body.read > int(MaxMediaBytes)+1 {
				t.Fatalf("unclosed or unbounded discovery body: closed=%v, read=%d", body.closed, body.read)
			}
			if scenario == "success" {
				if err != nil || !reflect.DeepEqual(names, []string{"raw@owner"}) {
					t.Fatal(names, err)
				}
			} else if err == nil || names != nil || scenario == "cancel" && !errors.Is(err, context.Canceled) {
				t.Fatal("discovery failure returned inventory", names, err)
			}
		})
	}
}
