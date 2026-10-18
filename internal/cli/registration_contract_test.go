package cli

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"testing"
)

func TestRegisterAndLogoutRESTContract(t *testing.T) {
	for _, envelope := range []string{"go", "python"} {
		t.Run(envelope, func(t *testing.T) {
			reject := false
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch r.URL.Path {
				case "/api/v1/users":
					if r.Method != "POST" || r.Header.Get("Authorization") != "" {
						t.Error("registration must be public POST")
					}
					var payload map[string]string
					if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
						t.Error(err)
						return
					}
					encrypted, err := base64.StdEncoding.DecodeString(payload["password"])
					if err != nil || len(encrypted) < 128 || payload["email"] != "owned@example.invalid" || payload["nickname"] != "Owned" {
						t.Error("wrong registration identity or unencrypted password")
					}
				case "/api/v1/auth/logout":
					if r.Method != "POST" || r.Header.Get("Authorization") != "owned-session" {
						t.Error("wrong logout contract")
					}
				default:
					t.Errorf("unexpected route %s", r.URL.Path)
				}
				code := 0
				if reject {
					code = 109
				}
				if envelope == "go" {
					fmt.Fprintf(w, `{"code":%d,"message":"result"}`, code)
				} else {
					fmt.Fprintf(w, `{"retcode":%d,"retmsg":"result"}`, code)
				}
			}))
			defer server.Close()
			parsed, _ := url.Parse(server.URL)
			port, _ := strconv.Atoi(parsed.Port())
			client := NewMultiRAGClient("user")
			client.HTTPClient.Host = parsed.Hostname()
			client.HTTPClient.Port = port
			client.HTTPClient.LoginToken = "owned-session"
			command, err := NewParser(`REGISTER USER 'owned@example.invalid' AS 'Owned' PASSWORD 'owned-password';`).Parse(false)
			if err != nil {
				t.Fatal(err)
			}
			if _, err = client.ExecuteCommand(command); err != nil {
				t.Fatal(err)
			}
			if client.HTTPClient.LoginToken != "owned-session" {
				t.Fatal("registration changed session")
			}
			reject = true
			if _, err = client.ExecuteCommand(command); err == nil {
				t.Fatal("rejected registration succeeded")
			}
			if _, err = client.Logout(); err == nil {
				t.Fatal("rejected logout succeeded")
			}
			if client.HTTPClient.LoginToken != "owned-session" {
				t.Fatal("rejected logout cleared session")
			}
			reject = false
			if _, err = client.Logout(); err != nil {
				t.Fatal(err)
			}
			if client.HTTPClient.LoginToken != "" {
				t.Fatal("logout retained session")
			}
		})
	}
}
