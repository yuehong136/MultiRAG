package cli

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"testing"
)

func TestAdminVariableGrammarAndMode(t *testing.T) {
	for _, value := range []struct{ input, want string }{{"0", "0"}, {"false", "false"}, {"'-1'", "-1"}, {"'+1'", "+1"}, {"1.5", "1.5"}, {"'local host'", "local host"}, {"''", ""}, {`'{"enabled":false}'`, `{"enabled":false}`}} {
		cmd, err := NewParser("SET VAR mail.value " + value.input + ";").Parse(AdminMode)
		if err != nil || cmd.Type != "set_variable" || cmd.Params["var_name"] != "mail.value" || cmd.Params["var_value"] != value.want {
			t.Fatalf("%+v: %+v %v", value, cmd, err)
		}
	}
	for _, text := range []string{"LIST VARS;", "SHOW VAR mail.;", "SET VAR mail.value 0;"} {
		cmd, err := NewParser(text).Parse(AdminMode)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := newConsumerCLI("user").ExecuteCommand(cmd); err == nil {
			t.Fatal("user mode accepted admin variable command")
		}
	}
	for _, text := range []string{"SET VAR mail.value 1.2.3;", "SET VAR mail.port;", "SET VAR mail.port 0 extra;", "SET VAR mail.port -;", "SET VAR mail.port 0; extra;", "SHOW VAR mail extra;", "SHOW VAR mail; extra;", "LIST VARS extra;", "LIST VARS; extra;"} {
		if _, err := NewParser(text).Parse(AdminMode); err == nil {
			t.Fatal("malformed grammar accepted", text)
		}
	}
}

func TestAdminVariablesQueryAndAcknowledgement(t *testing.T) {
	for _, tc := range []struct {
		body      string
		wantError bool
	}{{`{"code":0,"data":[]}`, false}, {`{"retcode":0,"data":[]}`, true}, {`{}`, true}, {`{"code":false,"data":[]}`, true}, {`{"code":0,"retcode":1,"data":[]}`, false}, {`{"code":0,"data":null}`, true}, {`{"code":400,"message":"denied"}`, true}} {
		t.Run(tc.body, func(t *testing.T) {
			server := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != "GET" || r.URL.Path != "/api/v1/admin/variables" || r.Header.Get("Authorization") != "owned-admin" {
					t.Error("incorrect admin query contract")
				}
				var body map[string]interface{}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body["var_name"] != "mail." {
					t.Error("canonical GET body changed", body, err)
				}
				fmt.Fprint(w, tc.body)
			}))
			defer server.Close()
			address, _ := url.Parse(server.URL)
			port, _ := strconv.Atoi(address.Port())
			client := newConsumerCLI("admin")
			consumerHTTP(client).Host = address.Hostname()
			consumerHTTP(client).Port = port
			consumerHTTP(client).LoginToken = consumerTokenPtr("owned-admin")
			cmd, err := NewParser("SHOW VAR mail.;").Parse(AdminMode)
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
