package cli

import (
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"testing"
)

func TestAdminVariableGrammarAndMode(t *testing.T) {
	for _, value := range []struct{ input, want string }{{"0", "0"}, {"false", "false"}, {"-1", "-1"}, {"+1", "+1"}, {"1.5", "1.5"}, {"'local host'", "local host"}, {"''", ""}, {`'{"enabled":false}'`, `{"enabled":false}`}} {
		cmd, err := NewParser("SET VAR mail.value " + value.input + ";").Parse(true)
		if err != nil || cmd.Type != "set_variable" || cmd.Params["var_name"] != "mail.value" || cmd.Params["var_value"] != value.want {
			t.Fatalf("%+v: %+v %v", value, cmd, err)
		}
	}
	for _, text := range []string{"LIST VARS;", "SHOW VAR mail.;", "SET VAR mail.value 0;"} {
		cmd, err := NewParser(text).Parse(true)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := NewMultiRAGClient("user").ExecuteCommand(cmd); err == nil {
			t.Fatal("user mode accepted admin variable command")
		}
	}
	for _, text := range []string{"SET VAR mail.value 1.2.3;", "SET VAR mail.port;", "SET VAR mail.port 0 extra;", "SET VAR mail.port 0", "SET VAR mail.port -;", "SET VAR mail.port 0; extra;", "SHOW VAR mail extra;", "SHOW VAR mail; extra;", "LIST VARS extra;", "LIST VARS; extra;"} {
		if _, err := NewParser(text).Parse(true); err == nil {
			t.Fatal("malformed grammar accepted", text)
		}
	}
}

func TestAdminVariablesQueryAndAcknowledgement(t *testing.T) {
	for _, tc := range []struct {
		body      string
		wantError bool
	}{{`{"code":0,"data":[]}`, false}, {`{"retcode":0,"data":[]}`, false}, {`{}`, true}, {`{"code":false,"data":[]}`, true}, {`{"code":0,"retcode":1,"data":[]}`, true}, {`{"code":0,"data":null}`, true}, {`{"code":400,"message":"denied"}`, true}} {
		t.Run(tc.body, func(t *testing.T) {
			server := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != "GET" || r.URL.Path != "/api/v1/admin/variables" || r.URL.Query().Get("var_name") != "mail." || r.ContentLength > 0 || r.Header.Get("Authorization") != "owned-admin" {
					t.Error("incorrect admin query contract")
				}
				fmt.Fprint(w, tc.body)
			}))
			defer server.Close()
			address, _ := url.Parse(server.URL)
			port, _ := strconv.Atoi(address.Port())
			client := NewMultiRAGClient("admin")
			client.HTTPClient.Host = address.Hostname()
			client.HTTPClient.Port = port
			client.HTTPClient.LoginToken = "owned-admin"
			cmd, err := NewParser("SHOW VAR mail.;").Parse(true)
			if err != nil {
				t.Fatal(err)
			}
			_, err = client.ExecuteCommand(cmd)
			if (err != nil) != tc.wantError {
				t.Fatalf("error=%v wanted=%v", err, tc.wantError)
			}
		})
	}
}
