package cli

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strconv"
	"strings"
	"testing"
)

func TestDropInstanceModelGrammarAndMode(t *testing.T) {
	for _, input := range []string{
		`DROP MODEL 'org/model' FROM 'vllm' 'local';`,
		`DROP MODEL 'org/model' FROM PROVIDER 'vllm' INSTANCE 'local';`,
		`DROP MODEL 'org/model' FROM 'vllm' 'local'`,
	} {
		cmd, err := NewParser(input).Parse(false)
		if err != nil || cmd.Type != "drop_instance_model" || cmd.Params["model_name"] != "org/model" || cmd.Params["instance_name"] != "local" {
			t.Fatalf("%v %v", cmd, err)
		}
		if _, err := NewParser(input).Parse(true); err == nil {
			t.Fatal("admin accepted user model deletion")
		}
	}
	for _, input := range []string{`DROP MODEL 'm' FROM 'p';`, `DROP MODEL 'm' 'p' 'i';`, `DROP MODEL 'm' FROM 'p' 'i' garbage;`, `DROP MODEL 'm' FROM 'p' 'i'; garbage`, `DROP MODEL PROVIDER 'p';`} {
		if _, err := NewParser(input).Parse(false); err == nil {
			t.Fatalf("accepted %s", input)
		}
	}
	if _, err := NewParser(`DROP MODEL PROVIDER 'p';`).Parse(true); err == nil {
		t.Fatal("dead admin command still parses")
	}
	cmd, err := NewParser(`ADD MODEL 'm' TO PROVIDER 'vllm' INSTANCE 'local' WITH TOKENS 1024 VISION CHAT EMBEDDING THINK;`).Parse(false)
	if err != nil || !reflect.DeepEqual(cmd.Params["model_types"], []string{"image2text", "chat", "embedding"}) || cmd.Params["model_type"] != "image2text" {
		t.Fatalf("%v %v", cmd, err)
	}
}

func TestDropInstanceModelClientErrorsAndEscaping(t *testing.T) {
	for _, test := range []struct {
		status  int
		body    string
		success bool
	}{
		{200, `{"code":0,"message":"success"}`, true},
		{200, `{"code":109,"message":"secret-response"}`, false},
		{200, `{}`, false}, {200, `null`, false}, {200, `secret-response`, false}, {500, `secret-response`, false},
	} {
		t.Run(test.body, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != "DELETE" || r.URL.EscapedPath() != "/api/v1/providers/p%3F%23/instances/local%20name/models" {
					t.Errorf("%s %s", r.Method, r.URL.EscapedPath())
				}
				var body struct {
					Models []string `json:"models"`
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil || !reflect.DeepEqual(body.Models, []string{"org/model"}) {
					t.Errorf("%v %v", body, err)
				}
				w.WriteHeader(test.status)
				w.Write([]byte(test.body))
			}))
			defer server.Close()
			u, _ := url.Parse(server.URL)
			port, _ := strconv.Atoi(u.Port())
			client := NewMultiRAGClient("user")
			client.HTTPClient.Host = u.Hostname()
			client.HTTPClient.Port = port
			client.HTTPClient.LoginToken = "fixture"
			cmd := NewCommand("drop_instance_model")
			cmd.Params["provider_name"] = "p?#"
			cmd.Params["instance_name"] = "local name"
			cmd.Params["model_name"] = "org/model"
			_, err := client.ExecuteUserCommand(cmd)
			if (err == nil) != test.success || (err != nil && strings.Contains(err.Error(), "secret-response")) {
				t.Fatal(err)
			}
			client.ServerType = "admin"
			if _, err := client.DropInstanceModel(cmd); err == nil {
				t.Fatal("admin request sent")
			}
		})
	}
}
