package cli

import (
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"testing"
)

func TestProviderMutationsAuthenticateAndReturnNilOnFailure(t *testing.T) {
	commands := []string{`CREATE PROVIDER 'Xinference' INSTANCE 'one' KEY '' URL 'http://localhost:9997';`, `ADD MODEL 'version' TO PROVIDER 'Replicate' INSTANCE 'one' WITH TOKENS 2048 CHAT;`, `DROP MODEL 'version' FROM PROVIDER 'Replicate' INSTANCE 'one';`, `DROP INSTANCE 'one' FROM 'Replicate';`}
	for _, credential := range []string{"api", "login"} {
		for i, syntax := range commands {
			for _, reply := range []struct {
				name, body string
				status     int
				success    bool
			}{{"success", `{"code":0,"message":"ok"}`, 200, true}, {"missing-code", `{"message":"missing"}`, 200, false}, {"business-error", `{"code":403,"message":"denied"}`, 200, false}, {"http-error", `{"code":0}`, 502, false}} {
				t.Run(fmt.Sprintf("%s/%d/%s", credential, i, reply.name), func(t *testing.T) {
					srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						if r.Header.Get("Authorization") != "Bearer owned-token" {
							t.Error("CLI credential omitted", r.Header.Get("Authorization"))
						}
						w.WriteHeader(reply.status)
						fmt.Fprint(w, reply.body)
					}))
					defer srv.Close()
					u, _ := url.Parse(srv.URL)
					host, port, _ := net.SplitHostPort(u.Host)
					number, _ := strconv.Atoi(port)
					client := NewMultiRAGClient("user")
					client.HTTPClient.Host = host
					client.HTTPClient.Port = number
					if credential == "api" {
						client.HTTPClient.APIKey = "owned-token"
					} else {
						client.HTTPClient.LoginToken = "owned-token"
					}
					cmd, err := NewParser(syntax).Parse(false)
					if err != nil {
						t.Fatal(syntax, err)
					}
					response, err := client.ExecuteUserCommand(cmd)
					if reply.success {
						if err != nil || response == nil {
							t.Fatal(response, err)
						}
						response.SetOutputFormat("json")
					} else if err == nil || response != nil {
						t.Fatal("failure returned a typed nil or fake success", response, err)
					}
				})
			}
		}
	}
}

func TestProviderMutationsRejectMissingCredentialsBeforeHTTP(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls++; w.WriteHeader(500) }))
	defer srv.Close()
	u, _ := url.Parse(srv.URL)
	host, port, _ := net.SplitHostPort(u.Host)
	number, _ := strconv.Atoi(port)
	client := NewMultiRAGClient("user")
	client.HTTPClient.Host = host
	client.HTTPClient.Port = number
	for _, syntax := range []string{`CREATE PROVIDER 'Xinference' INSTANCE 'one' KEY '' URL 'http://localhost:9997';`, `ADD MODEL 'version' TO PROVIDER 'Replicate' INSTANCE 'one' WITH TOKENS 2048 CHAT;`, `DROP MODEL 'version' FROM PROVIDER 'Replicate' INSTANCE 'one';`, `DROP INSTANCE 'one' FROM 'Replicate';`} {
		cmd, err := NewParser(syntax).Parse(false)
		if err != nil {
			t.Fatal(err)
		}
		if result, err := client.ExecuteUserCommand(cmd); err == nil || result != nil {
			t.Fatal(result, err)
		}
	}
	if calls != 0 {
		t.Fatal("missing credentials reached HTTP", calls)
	}
}
