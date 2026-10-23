package cli

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"testing"
)

func TestProviderDiscoveryAndConnectionTokenAuth(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer fixture" {
			t.Error("provider request lost token")
		}
		fmt.Fprint(w, `{"code":0,"data":[{"model_name":"running"}]}`)
	}))
	defer server.Close()
	u, _ := url.Parse(server.URL)
	port, _ := strconv.Atoi(u.Port())
	for _, login := range []bool{false, true} {
		c := NewMultiRAGClient("user")
		c.HTTPClient.Host = u.Hostname()
		c.HTTPClient.Port = port
		if login {
			c.HTTPClient.LoginToken = "fixture"
		} else {
			c.HTTPClient.APIKey = "fixture"
		}
		for _, syntax := range []string{`LIST SUPPORTED MODELS FROM "LocalAI" "one";`, `CHECK INSTANCE "one" FROM "LocalAI";`} {
			cmd, err := NewParser(syntax).Parse(false)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := c.ExecuteUserCommand(cmd); err != nil {
				t.Fatal(err)
			}
		}
	}
}
