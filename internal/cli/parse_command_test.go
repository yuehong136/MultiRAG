package cli

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"testing"
)

func TestParseAndProviderTaskSyntax(t *testing.T) {
	for input, kind := range map[string]string{
		"PARSE WITH 'MinerU2.5@one@Gitee' FILE 'document.pdf';":                 "parse_file_user_command",
		"PARSE WITH 'MinerU2.5@one@Gitee' URL 'https://files.invalid/doc.pdf';": "parse_file_user_command",
		"PARSE FILE 'document.pdf';":                                            "user_parse_local_file_command",
		"LIST 'one@Gitee' TASKS;":                                               "list_tasks_user_command",
		"SHOW 'one@Gitee' TASK 'job-1';":                                        "show_task_user_command",
	} {
		cmd, err := NewParser(input).Parse(APIMode)
		if err != nil || cmd.Type != kind {
			t.Fatalf("%s: %v %v", input, cmd, err)
		}
		if kind == "list_tasks_user_command" || kind == "show_task_user_command" {
			if cmd.Params["composite_instance_name"] != "one@Gitee" {
				t.Fatal("task composite not resolved")
			}
		}
	}
	for _, input := range []string{
		"PARSE WITH 'bad' FILE 'a';", "PARSE WITH 'm@@Gitee' FILE 'a';", "PARSE FILE 'a' URL 'https://files.invalid/x';",
		"PARSE FILE 'a'; ignored", "PARSE URL 'file:///secret';",
		"LIST 'one@Gitee' TASKS; ignored",
		"SHOW 'one@Gitee' TASK 'job-1'; ignored",
	} {
		if cmd, err := NewParser(input).Parse(APIMode); err == nil {
			t.Fatal("accepted invalid syntax", input, cmd)
		}
	}
}

func TestParseFileUsesSelectedModel(t *testing.T) {
	calls := 0
	srv := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body["model_name"] != "MinerU2.5" || body["provider_name"] != "Gitee" || body["instance_name"] != "one" || r.URL.Path != "/api/v1/file/parse" {
			t.Error("selected model lost", body)
		}
		fmt.Fprint(w, `{"code":0,"data":{"task_id":"job","status":"pending"}}`)
	}))
	defer srv.Close()
	u, _ := url.Parse(srv.URL)
	host, port, _ := net.SplitHostPort(u.Host)
	number, _ := strconv.Atoi(port)
	c := newConsumerCLI("user")
	consumerHTTP(c).Host = host
	consumerHTTP(c).Port = number
	consumerHTTP(c).LoginToken = consumerTokenPtr("controlled")
	for _, syntax := range []string{"USE MODEL 'MinerU2.5@one@Gitee';", "PARSE WITH 'MinerU2.5@one@Gitee' URL 'https://files.invalid/doc.pdf';"} {
		cmd, err := NewParser(syntax).Parse(APIMode)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := c.ExecuteUserCommand(cmd); err != nil {
			t.Fatal(err)
		}
	}
	c.CurrentModel = &CurrentModel{Model: "MinerU2.5", Instance: "", Provider: "Gitee"}
	cmd := NewCommand("parse_file_user_command")
	cmd.Params["url"] = "https://files.invalid/doc.pdf"
	if _, err := c.ExecuteUserCommand(cmd); err == nil || calls != 1 {
		t.Fatal("invalid selected model called provider", err, calls)
	}
}
