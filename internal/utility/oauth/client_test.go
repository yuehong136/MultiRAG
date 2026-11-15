package oauth

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strings"
	"testing"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
func protocolResponse(body string) *http.Response {
	return &http.Response{StatusCode: 200, Header: http.Header{}, Body: io.NopCloser(strings.NewReader(body))}
}
func TestCanonicalOAuthAuthorizationAndHTTP(t *testing.T) {
	cfg := Config{Type: "oauth2", ClientID: "client", ClientSecret: "private-fixture", AuthorizationURL: "https://issuer.invalid/auth?existing=1", TokenURL: "https://issuer.invalid/token", UserinfoURL: "https://issuer.invalid/profile", RedirectURI: "https://app.invalid/callback"}
	client, err := newOAuthClient(cfg)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := client.AuthorizationURL("state")
	if err != nil {
		t.Fatal(err)
	}
	parsed, _ := url.Parse(raw)
	if parsed.Query().Get("state") != "state" || parsed.Query().Get("existing") != "1" || strings.Contains(raw, cfg.ClientSecret) {
		t.Fatal("authorization parameters or secret boundary")
	}
	client.httpClient = &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		if request.URL.Path == "/token" {
			request.ParseForm()
			if request.PostForm.Get("client_secret") != cfg.ClientSecret || request.PostForm.Get("code") != "code" {
				t.Error("token form lost")
			}
			return protocolResponse(`{"access_token":"access","id_token":"ignored-claims"}`), nil
		}
		if request.Header.Get("Authorization") != "Bearer access" {
			t.Error("userinfo authentication lost")
		}
		return protocolResponse(`{"email":"member@example.invalid","preferred_username":"member"}`), nil
	})}
	token, err := client.ExchangeCodeForToken(t.Context(), "code")
	if err != nil {
		t.Fatal(err)
	}
	user, err := client.FetchUserInfo(t.Context(), token.AccessToken, token.IDToken)
	if err != nil || user.Email != "member@example.invalid" {
		t.Fatal(user, err)
	}
}
func TestCanonicalOAuthRequestFailures(t *testing.T) {
	for _, variant := range []string{"http", "redirect", "oversize", "malformed", "cancelled"} {
		t.Run(variant, func(t *testing.T) {
			client := &oauthClient{cfg: Config{TokenURL: "https://provider.invalid/token"}, httpClient: &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
				if request.Context().Err() != nil {
					return nil, request.Context().Err()
				}
				response := protocolResponse(`{"access_token":"access"}`)
				switch variant {
				case "http":
					response.StatusCode = 400
					response.Body = io.NopCloser(strings.NewReader("private-provider-error"))
				case "redirect":
					response.StatusCode = 302
				case "oversize":
					response.Body = io.NopCloser(strings.NewReader(strings.Repeat(" ", (1<<20)+1)))
				case "malformed":
					response.Body = io.NopCloser(strings.NewReader("{invalid"))
				}
				return response, nil
			})}}
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			if variant == "cancelled" {
				cancel()
			}
			result, err := client.ExchangeCodeForToken(ctx, "code")
			if err == nil || result != nil || strings.Contains(err.Error(), "private-provider-error") {
				t.Fatal("invalid response or secret detail accepted", err)
			}
			if variant == "cancelled" && !errors.Is(err, context.Canceled) {
				t.Fatal("cancellation lost", err)
			}
		})
	}
}
func TestCanonicalGitHubVerifiedEmailRoute(t *testing.T) {
	client, err := newGitHubClient(Config{ClientID: "client", RedirectURI: "https://app.invalid/callback"})
	if err != nil {
		t.Fatal(err)
	}
	client.httpClient = &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		if request.Header.Get("Authorization") != "Bearer access" {
			t.Error("GitHub credentials lost")
		}
		if strings.HasSuffix(request.URL.Path, "/emails") {
			return protocolResponse(`[{"email":"unverified@example.invalid","primary":true,"verified":false},{"email":"verified@example.invalid","verified":true}]`), nil
		}
		return protocolResponse(`{"login":"member","name":"Member"}`), nil
	})}
	user, err := client.FetchUserInfo(t.Context(), "access", "")
	if err != nil || user.Email != "verified@example.invalid" || user.Username != "member" {
		t.Fatal(user, err)
	}
}
