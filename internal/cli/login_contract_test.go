package cli

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"testing"
)

func TestLoginCommandRESTContract(t *testing.T) {
	consumerPublicKey(t)
	for _, mode := range []string{"user", "admin"} {
		t.Run(mode, func(t *testing.T) {
			calls := 0
			reject := false
			server := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				prefix := "/api/v1"
				if mode == "admin" {
					prefix += "/admin"
				}
				if r.Method == "GET" && r.URL.Path == prefix+map[string]string{"user": "/system/ping", "admin": "/ping"}[mode] {
					if mode == "admin" {
						fmt.Fprint(w, `{"code":0,"message":"PONG"}`)
					} else {
						fmt.Fprint(w, "pong")
					}
					return
				}
				path := prefix + map[string]string{"user": "/auth/login", "admin": "/login"}[mode]
				if r.URL.Path != path || r.Method != "POST" || r.Header.Get("Authorization") != "" {
					t.Errorf("unexpected login request: %s %s", r.Method, r.URL.Path)
				}
				var payload map[string]string
				if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
					t.Fatal(err)
				}
				key := "email"
				if mode == "admin" {
					key = "email"
				}
				ciphertext, err := base64.StdEncoding.DecodeString(payload["password"])
				if payload[key] != "owned@example.invalid" || err != nil || len(ciphertext) < 128 || len(payload) != 2 {
					t.Error("wrong identity or unencrypted password")
				}
				if reject {
					fmt.Fprint(w, `{"code":109,"message":"denied"}`)
					return
				}
				w.Header().Set("Authorization", "owned-session")
				fmt.Fprint(w, `{"code":0}`)
			}))
			defer server.Close()
			parsed, _ := url.Parse(server.URL)
			port, _ := strconv.Atoi(parsed.Port())
			client := newConsumerCLI(mode)
			consumerHTTP(client).Host = parsed.Hostname()
			consumerHTTP(client).Port = port
			cmd, err := NewParser(map[string]string{"user": `LOGIN USER 'owned@example.invalid' PASSWORD 'owned-password';`, "admin": `LOGIN ADMIN 'owned@example.invalid' PASSWORD 'owned-password';`}[mode]).Parse(consumerMode(mode == "admin"))
			if err != nil {
				t.Fatal(err)
			}
			if _, err = client.ExecuteCommand(cmd); err != nil {
				t.Fatal(err)
			}
			if consumerToken(consumerHTTP(client).LoginToken) != "owned-session" || calls != 2 {
				t.Fatal("login did not establish a session")
			}
			reject = true
			if _, err = client.ExecuteCommand(cmd); err == nil {
				t.Fatal("business failure accepted")
			}
			if consumerToken(consumerHTTP(client).LoginToken) != "owned-session" {
				t.Fatal("failed login replaced existing session")
			}
		})
	}
}

func TestHTTPMethodsUseAPIBase(t *testing.T) {
	count := 0
	server := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		count++
		if r.URL.Path != "/api/v1/probe" || r.Header.Get("Authorization") != "Bearer owned-api-key" {
			t.Errorf("request contract: %s", r.URL.Path)
		}
		fmt.Fprint(w, `{"code":0}`)
	}))
	defer server.Close()
	parsed, _ := url.Parse(server.URL)
	port, _ := strconv.Atoi(parsed.Port())
	client := NewHTTPClient()
	client.Host = parsed.Hostname()
	client.Port = port
	client.APIToken = consumerTokenPtr("owned-api-key")
	if _, err := client.RequestJSON("GET", "/probe", "api", nil, nil); err != nil {
		t.Fatal(err)
	}
	result, err := client.RequestWithIterations("POST", "/probe", "api", nil, map[string]interface{}{"test": true}, 2)
	if err != nil || result.SuccessCount+result.FailureCount != 2 {
		t.Fatal(result, err)
	}
	stream, err := client.RequestStream("POST", "/probe", "api", nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = io.ReadAll(stream); err != nil {
		t.Fatal(err)
	}
	stream.Close()
	if count != 4 {
		t.Fatal(count)
	}
	client.VerifySSL = true
	if client.BuildURL("/probe") != "https://"+parsed.Host+"/api/v1/probe" {
		t.Fatal("TLS base mismatch")
	}
}

func TestRegisterGrammar(t *testing.T) {
	cmd, err := NewParser(`REGISTER USER 'owned@example.invalid' AS 'Owned' PASSWORD 'owned-password';`).Parse(APIMode)
	if err != nil || cmd.Type != "register_user" || cmd.Params["user_name"] != "owned@example.invalid" {
		t.Fatal(cmd, err)
	}
	for _, input := range []string{`REGISTER USER 'x' 'n' PASSWORD 'p';`, `REGISTER USER 'x' AS 'n' 'p';`} {
		if _, err := NewParser(input).Parse(APIMode); err == nil {
			t.Fatal("invalid registration accepted")
		}
	}
}
