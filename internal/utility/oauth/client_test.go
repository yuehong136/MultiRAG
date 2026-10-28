package oauth

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"github.com/golang-jwt/jwt/v4"
	"io"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
func protocolResponse(body string) *http.Response {
	return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body)), Header: http.Header{}}
}

func TestOIDCRejectsInvalidTokensAndSubject(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	cfg := Config{Type: "oidc", ClientID: "client", ClientSecret: "secret", Issuer: "https://issuer.invalid", TokenURL: "https://issuer.invalid/token", UserinfoURL: "https://issuer.invalid/userinfo", RedirectURI: "https://app.invalid/callback"}
	claims := idClaims{RegisteredClaims: jwt.RegisteredClaims{Issuer: cfg.Issuer, Subject: "subject", Audience: jwt.ClaimStrings{"client"}, ExpiresAt: jwt.NewNumericDate(time.Now().Add(time.Hour)), IssuedAt: jwt.NewNumericDate(time.Now())}, Nonce: "nonce"}
	for _, variant := range []string{"valid", "expired", "missing-exp", "issuer", "audience", "nonce", "subject", "unsigned", "bad-key", "multi-aud", "future-iat", "wrong-alg"} {
		t.Run(variant, func(t *testing.T) {
			current := claims
			switch variant {
			case "expired":
				current.ExpiresAt = jwt.NewNumericDate(time.Now().Add(-time.Hour))
			case "missing-exp":
				current.ExpiresAt = nil
			case "issuer":
				current.Issuer = "other"
			case "audience":
				current.Audience = jwt.ClaimStrings{"other"}
			case "nonce":
				current.Nonce = "other"
			case "multi-aud":
				current.Audience = jwt.ClaimStrings{"client", "other"}
			case "future-iat":
				current.IssuedAt = jwt.NewNumericDate(time.Now().Add(time.Hour))
			}
			token := jwt.NewWithClaims(jwt.SigningMethodRS256, current)
			token.Header["kid"] = "key"
			raw, _ := token.SignedString(key)
			if variant == "unsigned" {
				unsigned := jwt.NewWithClaims(jwt.SigningMethodNone, current)
				unsigned.Header["kid"] = "key"
				raw, _ = unsigned.SignedString(jwt.UnsafeAllowNoneSignatureType)
			}
			if variant == "wrong-alg" {
				other := jwt.NewWithClaims(jwt.SigningMethodHS256, current)
				other.Header["kid"] = "key"
				raw, _ = other.SignedString([]byte("secret"))
			}
			infoSub := "subject"
			if variant == "subject" {
				infoSub = "attacker"
			}
			c := &Client{cfg: cfg, jwksURI: "https://issuer.invalid/keys", http: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
				switch r.URL.Path {
				case "/token":
					body, _ := io.ReadAll(r.Body)
					form, _ := url.ParseQuery(string(body))
					if form.Get("code_verifier") != strings.Repeat("v", 64) || form.Get("client_secret") != "secret" {
						t.Fatal("missing PKCE or credentials")
					}
					data, _ := json.Marshal(map[string]string{"access_token": "provider-token", "token_type": "Bearer", "id_token": raw})
					return protocolResponse(string(data)), nil
				case "/keys":
					kid := "key"
					if variant == "bad-key" {
						kid = "other"
					}
					data, _ := json.Marshal(map[string]any{"keys": []map[string]string{{"kty": "RSA", "kid": kid, "alg": "RS256", "use": "sig", "n": base64.RawURLEncoding.EncodeToString(key.N.Bytes()), "e": "AQAB"}}})
					return protocolResponse(string(data)), nil
				case "/userinfo":
					if r.Header.Get("Authorization") != "Bearer provider-token" {
						t.Fatal("missing bearer")
					}
					return protocolResponse(`{"sub":"` + infoSub + `","email":"existing@account.invalid"}`), nil
				}
				t.Fatal("unexpected endpoint")
				return nil, ErrProtocol
			})}}
			identity, err := c.VerifyCode(context.Background(), "code", strings.Repeat("v", 64), "nonce")
			if variant == "valid" {
				if err != nil || identity.Subject != "subject" {
					t.Fatal("valid protocol refused", err)
				}
			} else if err != ErrProtocol {
				t.Fatal("unsafe token accepted", variant)
			}
		})
	}
}
func TestAuthorizationPKCEAndSafeConfiguration(t *testing.T) {
	cfg := Config{Type: "oauth2", ClientID: "client", AuthorizationURL: "https://issuer.invalid/auth?existing=1", TokenURL: "https://issuer.invalid/token", UserinfoURL: "https://issuer.invalid/profile", RedirectURI: "https://app.invalid/callback"}
	c, err := NewClient(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := c.AuthorizationURL("state", strings.Repeat("v", 64), "nonce")
	if err != nil {
		t.Fatal(err)
	}
	u, _ := url.Parse(raw)
	q := u.Query()
	if q.Get("state") != "state" || q.Get("code_challenge_method") != "S256" || q.Get("code_challenge") == "" || q.Get("code_verifier") != "" || q.Get("existing") != "1" {
		t.Fatal("unsafe authorization URL")
	}
	for _, endpoint := range []string{"http://issuer.invalid/token", "https://user:secret@issuer.invalid/token", "javascript:alert(1)", "https://issuer.invalid/token#secret"} {
		bad := cfg
		bad.TokenURL = endpoint
		if _, err := NewClient(context.Background(), bad); err == nil {
			t.Fatal("bad endpoint accepted", endpoint)
		}
	}
}

func TestOAuthRequestBoundaries(t *testing.T) {
	for _, variant := range []string{"provider-error", "redirect", "oversize", "malformed", "cancelled"} {
		t.Run(variant, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if variant == "cancelled" {
				cancel()
			}
			c := &Client{http: &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
				if req.Context().Err() != nil {
					return nil, req.Context().Err()
				}
				response := protocolResponse(`{"ok":true}`)
				switch variant {
				case "provider-error":
					response.StatusCode = 400
					response.Body = io.NopCloser(strings.NewReader("secret-provider-error"))
				case "redirect":
					response.StatusCode = 302
					response.Header.Set("Location", "https://attacker.invalid")
				case "oversize":
					response.Body = io.NopCloser(strings.NewReader(strings.Repeat(" ", (1<<20)+1)))
				case "malformed":
					response.Body = io.NopCloser(strings.NewReader("{invalid"))
				}
				return response, nil
			})}}
			var data map[string]any
			if err := c.request(ctx, "GET", "https://provider.invalid", "", "", &data); err != ErrProtocol {
				t.Fatal("unsafe response accepted or detail leaked", err)
			}
		})
	}
}
func TestGitHubUsesSubjectWithoutEmailLookup(t *testing.T) {
	c, err := NewClient(context.Background(), Config{Type: "github", ClientID: "client", RedirectURI: "https://app.invalid/callback"})
	if err != nil {
		t.Fatal(err)
	}
	calls := []string{}
	c.http.Transport = roundTripFunc(func(req *http.Request) (*http.Response, error) {
		calls = append(calls, req.URL.String())
		if req.Method == "POST" {
			return protocolResponse(`{"access_token":"token","token_type":"bearer"}`), nil
		}
		return protocolResponse(`{"id":12345,"email":"existing@account.invalid"}`), nil
	})
	identity, err := c.VerifyCode(context.Background(), "code", strings.Repeat("v", 64), "nonce")
	if err != nil || identity.Subject != "12345" || len(calls) != 2 || calls[1] != "https://api.github.com/user" {
		t.Fatal("GitHub must use subject only", err, calls)
	}
}
