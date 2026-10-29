package cli

import (
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"testing"
)

func TestCatalogParserAndTransport(t *testing.T) {
	for _, admin := range []bool{false, true} {
		for _, tc := range []struct{ syntax, kind string }{
			{`LIST MODELS;`, "list_all_models"},
			{`SHOW MODEL '组织/a+b?';`, "show_catalog_model"},
			{`SHOW MODEL 'one' FROM 'provider';`, "show_model"},
			{`LIST MODELS FROM 'provider';`, "list_provider_models"},
			{`LIST MODELS FROM 'provider' 'instance';`, "list_instance_models"},
			{`LIST SUPPORTED MODELS FROM 'provider' 'instance';`, "list_supported_models"},
		} {
			cmd, err := NewParser(tc.syntax).Parse(admin)
			if err != nil || cmd.Type != tc.kind {
				t.Fatalf("%q: %v, %v", tc.syntax, cmd, err)
			}
		}
		for _, syntax := range []string{`LIST MODELS`, `LIST MODELS; trailing`, `LIST MODELS;;`, `SHOW MODEL 'one'`, `SHOW MODEL 'one'; trailing`, `SHOW MODEL 'one';;`} {
			if _, err := NewParser(syntax).Parse(admin); err == nil {
				t.Fatalf("accepted %q", syntax)
			}
		}
	}
	server := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" || r.ContentLength > 0 {
			t.Error("catalog needs bodyless GET")
		}
		switch r.URL.Path {
		case "/api/v1/all-models", "/api/v1/admin/all-models":
			fmt.Fprint(w, `{"code":0,"data":[{"name":"static","max_tokens":0}],"total":1}`)
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
		client := NewMultiRAGClient(mode)
		client.HTTPClient.Host = u.Hostname()
		client.HTTPClient.Port = port
		client.HTTPClient.LoginToken = "fixture"
		for _, syntax := range []string{`LIST MODELS;`, `SHOW MODEL '组织/a+b?';`} {
			cmd, err := NewParser(syntax).Parse(mode == "admin")
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
