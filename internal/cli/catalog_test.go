package cli

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"testing"
)

func TestCatalogParserAndTransport(t *testing.T) {
	for _, admin := range []bool{false, true} {
		for _, tc := range []struct{ syntax, kind string }{
			{`LIST MODELS;`, "list_all_models"},
			{`SHOW MODEL '组织/a+b?';`, "show_model"},
			{`SHOW MODEL 'one' FROM 'provider';`, "show_provider_model"},
			{`LIST MODELS FROM 'provider';`, "list_provider_models"},
			{`LIST MODELS FROM 'provider' 'instance';`, "list_instance_models"},
			{`LIST SUPPORTED MODELS FROM 'provider' 'instance';`, "list_supported_models"},
		} {
			if admin && tc.kind == "list_supported_models" {
				continue
			}
			cmd, err := NewParser(tc.syntax).Parse(consumerMode(admin))
			if err != nil || cmd.Type != tc.kind {
				t.Fatalf("%q: %v, %v", tc.syntax, cmd, err)
			}
		}
		for _, syntax := range []string{`LIST MODELS; trailing`, `LIST MODELS;;`, `SHOW MODEL 'one'; trailing`, `SHOW MODEL 'one';;`} {
			if _, err := NewParser(syntax).Parse(consumerMode(admin)); err == nil {
				t.Fatalf("accepted %q", syntax)
			}
		}
	}
	server := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" {
			t.Error("catalog requires GET")
		}
		switch r.URL.Path {
		case "/api/v1/all-models":
			var payload map[string]interface{}
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				t.Error(err)
			}
			if name, ok := payload["model_name"]; ok {
				if name != "组织/a+b?" {
					t.Error("model identity changed", name)
				}
				fmt.Fprint(w, `{"code":0,"data":{"name":"static","max_tokens":0}}`)
			} else {
				fmt.Fprint(w, `{"code":0,"data":[{"name":"static","max_tokens":0}]}`)
			}
		case "/api/v1/providers/provider/models", "/api/v1/admin/providers/provider/models":
			fmt.Fprint(w, `{"code":0,"data":[{"name":"static","max_tokens":0}],"total":1}`)
		case "/api/v1/providers/provider/models/legacy", "/api/v1/admin/providers/provider/models/legacy":
			fmt.Fprint(w, `{"code":0,"data":{"name":"legacy"}}`)
		case "/api/v1/all-models/detail", "/api/v1/admin/all-models/detail":
			if r.URL.Query().Get("model_name") != "组织/a+b?" {
				t.Error("corrupted name")
			}
			fmt.Fprint(w, `{"code":0,"data":{"name":"static","max_tokens":0}}`)
		default:
			t.Errorf("wrong path %s", r.URL.Path)
			w.WriteHeader(404)
		}
		if r.Header.Get("Authorization") != "fixture" {
			t.Error("missing login identity")
		}
	}))
	u, _ := url.Parse(server.URL)
	port, _ := strconv.Atoi(u.Port())
	for _, mode := range []string{"admin", "user"} {
		client := newConsumerCLI(mode)
		consumerHTTP(client).Host = u.Hostname()
		consumerHTTP(client).Port = port
		consumerHTTP(client).LoginToken = consumerTokenPtr("fixture")
		for _, syntax := range []string{`LIST MODELS;`, `SHOW MODEL '组织/a+b?';`, `LIST MODELS FROM 'provider';`, `SHOW MODEL 'legacy' FROM 'provider';`} {
			cmd, err := NewParser(syntax).Parse(consumerMode(mode == "admin"))
			if err != nil {
				t.Fatal(err)
			}
			result, err := client.ExecuteCommand(cmd)
			if err != nil {
				t.Fatal(err)
			}
			result.SetOutputFormat(OutputFormatJSON)
			if result == nil {
				t.Fatal("nil response")
			}
		}
	}
}

func TestCatalogResponseFailures(t *testing.T) {
	for _, mode := range []string{"user", "admin"} {
		for _, tc := range []struct {
			body             string
			status           int
			listOK, detailOK bool
		}{
			{`null`, 200, false, false}, {`{}`, 200, false, false}, {`{"message":"failure"}`, 200, false, false},
			{`{"code":null,"data":[]}`, 200, false, false}, {`{"code":0}`, 200, false, false},
			{`{"code":0,"data":null}`, 200, false, false}, {`{"code":0,"data":[null]}`, 200, false, false},
			{`{"code":0,"data":{}}`, 200, false, false}, {`{"code":0,"data":{"name":42}}`, 200, false, false},
			{`{"code":0,"data":{"name":" "}}`, 200, false, false}, {`{"code":0,"data":[{}]}`, 200, false, false},
			{`{"code":0,"data":[]}`, 200, true, false}, {`{"code":0,"data":{"name":"valid","max_tokens":0}}`, 200, false, true},
			{`{"code":400,"data":[],"message":"secret-key"}`, 200, false, false},
			{`secret-key`, 500, false, false}, {`{`, 200, false, false},
		} {
			server := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(tc.status); fmt.Fprint(w, tc.body) }))
			u, _ := url.Parse(server.URL)
			port, _ := strconv.Atoi(u.Port())
			client := newConsumerCLI(mode)
			consumerHTTP(client).Host = u.Hostname()
			consumerHTTP(client).Port = port
			consumerHTTP(client).LoginToken = consumerTokenPtr("fixture")
			for _, detail := range []bool{false, true} {
				cmd := NewCommand("list_all_models")
				want := tc.listOK
				if detail {
					cmd = NewCommand("show_model")
					cmd.Params["model_name"] = "valid"
					want = tc.detailOK
				}
				_, err := client.ExecuteCommand(cmd)
				if (err == nil) != want {
					t.Fatalf("mode %s detail %v body %s: %v", mode, detail, tc.body, err)
				}
				if err != nil && strings.Contains(err.Error(), "secret-key") {
					t.Fatal("response leaked into error")
				}
			}
		}
	}
}
