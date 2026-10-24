package cli

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"testing"
)

func TestParseFileURLAliasDoesNotReadOrFetchFile(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Path != "/api/v1/file/parse" {
			t.Error(r.URL)
		}
		var body map[string]interface{}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body["url"] != "https://files.invalid/no-such-document.pdf" || body["content"] != nil {
			t.Error("URL alias sent local bytes", body)
		}
		fmt.Fprint(w, `{"code":0,"data":{"task_id":"owned-job","status":"pending"}}`)
	}))
	defer srv.Close()
	u, _ := url.Parse(srv.URL)
	host, port, _ := net.SplitHostPort(u.Host)
	number, _ := strconv.Atoi(port)
	client := NewMultiRAGClient("user")
	client.HTTPClient.Host = host
	client.HTTPClient.Port = number
	client.HTTPClient.LoginToken = "controlled"
	cmd, err := NewParser("PARSE WITH 'pipeline@one@MinerU' FILE 'https://files.invalid/no-such-document.pdf';").Parse(false)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = client.ExecuteUserCommand(cmd); err != nil || calls != 1 {
		t.Fatal(err, calls)
	}
	cmd.Params["file"] = "https://user:password@files.invalid/no.pdf"
	if _, err = client.ExecuteUserCommand(cmd); err == nil || calls != 1 {
		t.Fatal("invalid URL accepted", err, calls)
	}
}
