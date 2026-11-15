package cli

import (
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"testing"
)

func TestAdminTasksGrammarAndModeBoundary(t *testing.T) {
	for _, input := range []string{"LIST TASKS;", "list tasks;"} {
		cmd, err := NewParser(input).Parse(AdminMode)
		if err != nil || cmd.Type != "list_admin_tasks" {
			t.Fatal(cmd, err)
		}
		if _, err = NewParser(input).Parse(APIMode); err == nil {
			t.Fatal("user mode accepted admin command")
		}
	}
	for _, input := range []string{"LIST TASKS extra;"} {
		if _, err := NewParser(input).Parse(AdminMode); err == nil {
			t.Fatalf("invalid task grammar accepted: %s", input)
		}
	}
	for _, input := range []string{"LIST USERS;", "LIST SERVICES;", "LIST ROLES;"} {
		if _, err := NewParser(input).Parse(APIMode); err == nil {
			t.Fatalf("user mode accepted %s", input)
		}
		if _, err := NewParser(input).Parse(AdminMode); err != nil {
			t.Fatal(err)
		}
	}
	client := newConsumerCLI("user")
	if _, err := client.ListAdminTasks(NewCommand("list_admin_tasks")); err == nil {
		t.Fatal("direct task command bypassed mode boundary")
	}
	if _, err := client.ExecuteCommand(NewCommand("list_admin_tasks")); err == nil {
		t.Fatal("user dispatch accepted task command")
	}
}

func TestAdminTasksHTTPContract(t *testing.T) {
	for _, test := range []struct {
		status    int
		body      string
		wantError bool
	}{
		{200, `{"code":0,"data":[{"id":"task","chunk_count":2}]}`, false},
		{200, `{"code":0,"data":[]}`, false},
		{200, `{"code":403,"message":"denied"}`, true},
		{403, `{"code":0}`, true}, {500, `{"code":500,"message":"database failed"}`, true},
		{200, `{}`, true}, {200, `not JSON`, true}, {200, `{"code":0,"data":"invalid"}`, true},
	} {
		t.Run(test.body, func(t *testing.T) {
			calls := 0
			server := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				if r.Method != http.MethodGet || r.URL.Path != "/api/v1/admin/tasks" || r.Header.Get("Authorization") != "owned-admin-session" {
					t.Error("wrong admin request contract")
				}
				w.WriteHeader(test.status)
				fmt.Fprint(w, test.body)
			}))
			defer server.Close()
			address, _ := url.Parse(server.URL)
			port, _ := strconv.Atoi(address.Port())
			client := newConsumerCLI("admin")
			consumerHTTP(client).Host = address.Hostname()
			consumerHTTP(client).Port = port
			consumerHTTP(client).LoginToken = consumerTokenPtr("owned-admin-session")
			cmd, err := NewParser("LIST TASKS;").Parse(AdminMode)
			if err != nil {
				t.Fatal(err)
			}
			result, err := client.ExecuteCommand(cmd)
			if (err != nil) != test.wantError {
				t.Fatalf("result %v error %v", result, err)
			}
			if err != nil && strings.Contains(err.Error(), "drop token") {
				t.Fatal("unrelated operation in error")
			}
			if calls != 1 || consumerToken(consumerHTTP(client).LoginToken) != "owned-admin-session" {
				t.Fatal("task listing altered session or request count")
			}
		})
	}
}
