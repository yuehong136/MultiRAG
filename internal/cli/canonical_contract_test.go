package cli

import (
	"bytes"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestCanonicalHTTPRejectsFalseSuccessAndRedactsBodies(t *testing.T) {
	for _, tc := range []struct {
		status int
		body   string
	}{{403, `credential-canary`}, {500, `{"code":0}`}, {200, `{"message":"credential-canary"}`}, {200, `{"code":null}`}, {200, `{"code":"credential-canary"}`}, {200, `{"code":109,"message":"credential-canary"}`}, {200, `{`}} {
		t.Run(fmt.Sprint(tc.status, tc.body), func(t *testing.T) {
			client := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(tc.status)
				io.WriteString(w, tc.body)
			})
			if result, err := client.Request("GET", "/controlled", "api", nil, nil); err == nil || result != nil || strings.Contains(err.Error(), "credential-canary") {
				t.Fatal("response failure acknowledged or leaked", err)
			}
		})
	}
	client := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Content-Disposition", "attachment; filename=file.json")
		io.WriteString(w, `{"document":true}`)
	})
	if result, err := client.Request("GET", "/controlled", "api", nil, nil); err != nil || string(result.Body) != `{"document":true}` {
		t.Fatal("attachment was not preserved", err)
	}
}

func TestCanonicalMultipartRejectsMalformedAcknowledgement(t *testing.T) {
	for _, body := range []string{`not-json`, `{}`, `{"code":null}`, `{"code":109,"message":"credential-canary"}`} {
		client := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, body) })
		if err := client.UploadMultipart("/controlled", "application/octet-stream", bytes.NewBufferString("file")); err == nil || strings.Contains(err.Error(), "credential-canary") {
			t.Fatal("invalid upload acknowledgement", err)
		}
	}
}

func TestCanonicalServerRegistryUsesNamedClients(t *testing.T) {
	c := newConsumerCLI("user")
	consumerHTTP(c).LoginToken = consumerTokenPtr("current-session")
	cmd := NewCommand("add_api_server")
	cmd.Params["server_name"] = "second"
	cmd.Params["server_ip"] = "127.0.0.1"
	cmd.Params["server_port"] = 9480
	cmd.Params["server_token"] = "second-token"
	if _, err := c.AddAPIServer(cmd); err != nil {
		t.Fatal(err)
	}
	if c.Config.APIClientConfig.APIServerMap["second"].ApiToken == nil || consumerToken(c.APIServerClientMap["second"].APIToken) != "second-token" {
		t.Fatal("named token was not installed")
	}
	info, err := c.GetAPIServerInfo("second")
	if err != nil || info.(*CommonDataResponse).Data["auth"] != "api token" {
		t.Fatal("another profile's session leaked into named profile", info, err)
	}
	missing := NewCommand("use_api_server")
	missing.Params["server_name"] = "missing"
	if _, err := c.UseAPIServer(missing); err == nil || c.Config.APIClientConfig.CurrentAPIServer != "test" {
		t.Fatal("failed switch changed profile")
	}
	switchCmd := NewCommand("use_api_server")
	switchCmd.Params["server_name"] = "second"
	if _, err := c.UseAPIServer(switchCmd); err != nil {
		t.Fatal(err)
	}
	if c.Config.APIClientConfig.CurrentAPIServer != "second" || consumerToken(c.APIServerClientMap["test"].LoginToken) != "current-session" {
		t.Fatal("switch discarded profile state")
	}
	if _, err := c.SaveServerConfig(NewCommand("save_server_config")); err == nil {
		t.Fatal("unimplemented config save reported success")
	}
}

func TestCanonicalParserRejectsTrailingCommands(t *testing.T) {
	for _, input := range []string{`SHOW CURRENT; PING;`, `USE API 'test'; PING;`, `LIST MODELS;;`, `USE ADMIN trailing;`} {
		if _, err := NewParser(input).Parse(APIMode); err == nil {
			t.Fatal("ignored trailing input", input)
		}
	}
	for _, input := range []string{`USE API 'test'`, `USE API 'test';`, `USE ADMIN`, `USE ADMIN;`} {
		if _, err := NewParser(input).Parse(APIMode); err != nil {
			t.Fatal("target optional semicolon rejected", input, err)
		}
	}
}
