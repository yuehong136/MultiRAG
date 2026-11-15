package cli

import (
	"fmt"
	"net/http"
	"testing"
)

func TestProviderBalanceAuthenticatesAndRejectsFalseSuccess(t *testing.T) {
	for _, credential := range []string{"api", "login"} {
		for _, test := range []struct {
			body  string
			valid bool
		}{{`{"code":0,"data":{"balance":0,"currency":"USD"}}`, true}, {`{"data":{"balance":0}}`, false}, {`{"code":403,"message":"denied"}`, false}, {`{"code":0}`, false}} {
			t.Run(credential+test.body, func(t *testing.T) {
				client := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) {
					if r.Method != "GET" || r.URL.Path != "/api/v1/providers/Novita/instances/one/balance" || r.Header.Get("Authorization") != map[string]string{"api": "Bearer test-credential", "login": "test-credential"}[credential] || r.ContentLength != 0 {
						t.Error("balance request lost auth/path")
					}
					fmt.Fprint(w, test.body)
				})
				if credential == "login" {
					client.APIToken = nil
					client.LoginToken = consumerTokenPtr("test-credential")
				}
				c := consumerCLIWithHTTP(client)
				cmd, err := NewParser(`SHOW BALANCE FROM 'Novita' 'one';`).Parse(APIMode)
				if err != nil {
					t.Fatal(err)
				}
				response, err := c.ShowInstanceBalance(cmd)
				if test.valid {
					if err != nil || response == nil {
						t.Fatal(response, err)
					}
				} else if err == nil || response != nil {
					t.Fatal("false success", response, err)
				}
			})
		}
	}
}
