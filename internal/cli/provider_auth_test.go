package cli

import (
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"testing"
)

func TestProviderDiscoveryAndConnectionTokenAuth(t *testing.T) {
	server := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer fixture" && r.Header.Get("Authorization") != "fixture" {
			t.Error("provider request lost token")
		}
		fmt.Fprint(w, `{"code":0,"data":[{"name":"running"}]}`)
	}))
	defer server.Close()
	u, _ := url.Parse(server.URL)
	port, _ := strconv.Atoi(u.Port())
	for _, login := range []bool{false, true} {
		c := newConsumerCLI("user")
		consumerHTTP(c).Host = u.Hostname()
		consumerHTTP(c).Port = port
		if login {
			consumerHTTP(c).LoginToken = consumerTokenPtr("fixture")
		} else {
			consumerHTTP(c).APIToken = consumerTokenPtr("fixture")
		}
		for _, syntax := range []string{`LIST SUPPORTED MODELS FROM "LocalAI" "one";`, `CHECK INSTANCE "one" FROM "LocalAI";`} {
			cmd, err := NewParser(syntax).Parse(APIMode)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := c.ExecuteUserCommand(cmd); err != nil {
				t.Fatal(err)
			}
		}
	}
}
