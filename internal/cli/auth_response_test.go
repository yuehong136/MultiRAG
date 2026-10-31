package cli

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestAuthFailureDiagnosticsExcludeResponseText(t *testing.T) {
	for _, test := range []struct {
		name string
		body string
		code int
		want string
	}{
		{"go business code", `{"code":109,"message":"credential-canary"}`, 200, "login failed: business code 109"},
		{"python business code", `{"retcode":401,"retmsg":"credential-canary"}`, 200, "login failed: business code 401"},
		{"HTTP failure", `credential-canary`, 403, "login failed: HTTP 403"},
		{"invalid envelope", `{"code":"credential-canary"}`, 200, "login failed: invalid JSON response"},
		{"numeric overflow", `{"code":123456789012345678901234567890}`, 200, "login failed: invalid JSON response"},
		{"conflicting codes", `{"code":0,"retcode":109,"message":"credential-canary"}`, 200, "login failed: invalid business code"},
		{"missing codes", `{"message":"credential-canary"}`, 200, "login failed: invalid business code"},
	} {
		t.Run(test.name, func(t *testing.T) {
			err := checkLoginResponse(&Response{StatusCode: test.code, Body: []byte(test.body)})
			if err == nil || err.Error() != test.want {
				t.Fatalf("unexpected authentication diagnostic; want %q", test.want)
			}
			if strings.Contains(err.Error(), "credential-canary") {
				t.Fatal("authentication diagnostic exposed response text")
			}
		})
	}
	if err := checkLoginResponse(&Response{StatusCode: http.StatusOK, Body: []byte(`{"code":0,"message":"credential-canary"}`)}); err != nil {
		t.Fatal("successful business code rejected")
	}
}

type loginFailureTransport struct {
	method string
	cause  error
}

func (s loginFailureTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	if request.Method == s.method {
		return nil, s.cause
	}
	return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader("pong")), Header: make(http.Header)}, nil
}

func TestLoginFailureRetainsCauseWithoutEcho(t *testing.T) {
	cause := errors.New("credential-canary")
	for _, mode := range []string{"user", "admin"} {
		for _, method := range []string{http.MethodGet, http.MethodPost} {
			for _, interactive := range []bool{false, true} {
				client := NewMultiRAGClient(mode)
				client.HTTPClient.LoginToken = "retained-session"
				client.HTTPClient.client.Transport = loginFailureTransport{method: method, cause: cause}
				var err error
				if interactive {
					err = client.LoginUserInteractive("owned@example.invalid", "fixture-password")
				} else {
					cmd := NewCommand("login_user")
					cmd.Params["email"], cmd.Params["password"] = "owned@example.invalid", "fixture-password"
					err = client.LoginUser(cmd)
				}
				want := "login request failed"
				if method == http.MethodGet {
					want = "login ping failed"
				}
				if err == nil || err.Error() != want || !errors.Is(err, cause) {
					t.Fatal("login did not retain a redacted transport failure and its cause")
				}
				if client.HTTPClient.LoginToken != "retained-session" {
					t.Fatal("failed login replaced the active session")
				}
			}
		}
	}
	var typeError *json.UnmarshalTypeError
	err := checkLoginResponse(&Response{StatusCode: 200, Body: []byte(`{"code":"credential-canary"}`)})
	if !errors.As(err, &typeError) {
		t.Fatal("redacted JSON error lost its cause")
	}
	if err := checkLoginResponse(nil); err == nil {
		t.Fatal("missing response accepted")
	}
	err = checkAuthResponse("resolve dataset", &Response{StatusCode: 200, Body: []byte(`{"code":109,"message":"dataset not found"}`)})
	if err == nil || err.Error() != "resolve dataset failed: dataset not found" {
		t.Fatal("non-login business diagnostic changed")
	}
}
