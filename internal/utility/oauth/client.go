// Package oauth implements bounded authorization-code protocol checks. It does
// not map provider profiles or email addresses to platform accounts.
package oauth

import (
	"context"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"math/big"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/golang-jwt/jwt/v4"
)

var ErrProtocol = errors.New("oauth_protocol_error")

// Config contains only server-controlled provider endpoints and credentials.
type Config struct {
	Type, ClientID, ClientSecret, AuthorizationURL, TokenURL, UserinfoURL, RedirectURI, Scope, Issuer string
}

type Client struct {
	cfg     Config
	http    *http.Client
	jwksURI string
}
type Identity struct{ Issuer, Subject string }
type tokenResponse struct {
	AccessToken string `json:"access_token"`
	IDToken     string `json:"id_token"`
	TokenType   string `json:"token_type"`
}

func validEndpoint(raw string) bool {
	u, err := url.Parse(raw)
	return err == nil && u.Scheme == "https" && u.Hostname() != "" && u.User == nil && u.Fragment == ""
}

// NewClient validates configuration without sending credentials to discovery.
func NewClient(ctx context.Context, cfg Config) (*Client, error) {
	c := &Client{cfg: cfg, http: &http.Client{Timeout: 7 * time.Second, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}}
	if cfg.ClientID == "" || !validEndpoint(cfg.RedirectURI) {
		return nil, ErrProtocol
	}
	if cfg.Type == "" {
		if cfg.Issuer != "" {
			cfg.Type = "oidc"
		} else {
			cfg.Type = "oauth2"
		}
	}
	switch cfg.Type {
	case "github":
		cfg.AuthorizationURL, cfg.TokenURL, cfg.UserinfoURL = "https://github.com/login/oauth/authorize", "https://github.com/login/oauth/access_token", "https://api.github.com/user"
		if cfg.Scope == "" {
			cfg.Scope = "read:user"
		}
	case "oidc":
		if !validEndpoint(cfg.Issuer) {
			return nil, ErrProtocol
		}
		var meta struct {
			Issuer   string `json:"issuer"`
			Auth     string `json:"authorization_endpoint"`
			Token    string `json:"token_endpoint"`
			Userinfo string `json:"userinfo_endpoint"`
			JWKS     string `json:"jwks_uri"`
		}
		if err := c.request(ctx, http.MethodGet, strings.TrimRight(cfg.Issuer, "/")+"/.well-known/openid-configuration", "", "", &meta); err != nil || meta.Issuer != cfg.Issuer || !validEndpoint(meta.JWKS) {
			return nil, ErrProtocol
		}
		cfg.AuthorizationURL, cfg.TokenURL, cfg.UserinfoURL = meta.Auth, meta.Token, meta.Userinfo
		c.jwksURI = meta.JWKS
		cfg.Scope = "openid " + cfg.Scope
	case "oauth2":
	default:
		return nil, ErrProtocol
	}
	if !validEndpoint(cfg.AuthorizationURL) || !validEndpoint(cfg.TokenURL) || !validEndpoint(cfg.UserinfoURL) {
		return nil, ErrProtocol
	}
	c.cfg = cfg
	return c, nil
}

func (c *Client) AuthorizationURL(state, verifier, nonce string) (string, error) {
	if state == "" || len(verifier) < 43 || nonce == "" {
		return "", ErrProtocol
	}
	u, _ := url.Parse(c.cfg.AuthorizationURL)
	q := u.Query()
	q.Set("client_id", c.cfg.ClientID)
	q.Set("redirect_uri", c.cfg.RedirectURI)
	q.Set("response_type", "code")
	q.Set("scope", c.cfg.Scope)
	q.Set("state", state)
	digest := sha256.Sum256([]byte(verifier))
	q.Set("code_challenge", base64.RawURLEncoding.EncodeToString(digest[:]))
	q.Set("code_challenge_method", "S256")
	if c.cfg.Type == "oidc" {
		q.Set("nonce", nonce)
	}
	u.RawQuery = q.Encode()
	return u.String(), nil
}

func (c *Client) request(ctx context.Context, method, endpoint, form, bearer string, dest any) error {
	req, err := http.NewRequestWithContext(ctx, method, endpoint, strings.NewReader(form))
	if err != nil {
		return ErrProtocol
	}
	req.Header.Set("Accept", "application/json")
	if method == http.MethodPost {
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	}
	if bearer != "" {
		req.Header.Set("Authorization", "Bearer "+bearer)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return ErrProtocol
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return ErrProtocol
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, (1<<20)+1))
	if err != nil || len(raw) > 1<<20 || json.Unmarshal(raw, dest) != nil {
		return ErrProtocol
	}
	return nil
}

// VerifyCode checks provider identity only. Callers must still require a
// persisted provider-subject binding before granting a platform login.
func (c *Client) VerifyCode(ctx context.Context, code, verifier, nonce string) (*Identity, error) {
	if code == "" || len(verifier) < 43 {
		return nil, ErrProtocol
	}
	form := url.Values{"client_id": {c.cfg.ClientID}, "client_secret": {c.cfg.ClientSecret}, "code": {code}, "redirect_uri": {c.cfg.RedirectURI}, "grant_type": {"authorization_code"}, "code_verifier": {verifier}}
	var token tokenResponse
	if c.request(ctx, http.MethodPost, c.cfg.TokenURL, form.Encode(), "", &token) != nil || token.AccessToken == "" || !strings.EqualFold(token.TokenType, "bearer") || strings.ContainsAny(token.AccessToken, "\r\n") {
		return nil, ErrProtocol
	}
	var profile map[string]json.RawMessage
	var subject string
	if c.cfg.Type == "oidc" {
		claims, err := c.verifyIDToken(ctx, token.IDToken, nonce)
		if err != nil {
			return nil, ErrProtocol
		}
		subject = claims.Subject
	}
	if c.request(ctx, http.MethodGet, c.cfg.UserinfoURL, "", token.AccessToken, &profile) != nil {
		return nil, ErrProtocol
	}
	if c.cfg.Type == "github" {
		var id json.Number
		if json.Unmarshal(profile["id"], &id) != nil || id.String() == "" {
			return nil, ErrProtocol
		}
		subject = id.String()
	} else {
		var sub string
		if json.Unmarshal(profile["sub"], &sub) != nil || sub == "" || (subject != "" && subject != sub) {
			return nil, ErrProtocol
		}
		subject = sub
	}
	issuer := c.cfg.Issuer
	if issuer == "" {
		issuer = c.cfg.UserinfoURL
	}
	return &Identity{Issuer: issuer, Subject: subject}, nil
}

type idClaims struct {
	jwt.RegisteredClaims
	Nonce           string `json:"nonce"`
	AuthorizedParty string `json:"azp"`
}

func (c *Client) verifyIDToken(ctx context.Context, raw, nonce string) (*idClaims, error) {
	if raw == "" || nonce == "" {
		return nil, ErrProtocol
	}
	var keys struct {
		Keys []struct{ Kty, Kid, Use, Alg, N, E string } `json:"keys"`
	}
	if c.request(ctx, http.MethodGet, c.jwksURI, "", "", &keys) != nil {
		return nil, ErrProtocol
	}
	claims := &idClaims{}
	parsed, err := jwt.ParseWithClaims(raw, claims, func(token *jwt.Token) (any, error) {
		kid, ok := token.Header["kid"].(string)
		if !ok || kid == "" || token.Method != jwt.SigningMethodRS256 {
			return nil, ErrProtocol
		}
		var selected *rsa.PublicKey
		for _, key := range keys.Keys {
			if key.Kid != kid || key.Kty != "RSA" || (key.Use != "" && key.Use != "sig") || (key.Alg != "" && key.Alg != "RS256") {
				continue
			}
			n, e1 := base64.RawURLEncoding.DecodeString(key.N)
			e, e2 := base64.RawURLEncoding.DecodeString(key.E)
			if e1 != nil || e2 != nil || len(e) == 0 || len(e) > 4 || len(n) < 256 || selected != nil {
				return nil, ErrProtocol
			}
			exponent := 0
			for _, b := range e {
				exponent = exponent*256 + int(b)
			}
			if exponent < 3 || exponent%2 == 0 {
				return nil, ErrProtocol
			}
			selected = &rsa.PublicKey{N: new(big.Int).SetBytes(n), E: exponent}
		}
		if selected == nil {
			return nil, ErrProtocol
		}
		return selected, nil
	}, jwt.WithValidMethods([]string{"RS256"}))
	if err != nil || parsed == nil || !parsed.Valid || claims.Issuer != c.cfg.Issuer || !claims.VerifyAudience(c.cfg.ClientID, true) || claims.ExpiresAt == nil || claims.IssuedAt == nil || claims.Subject == "" || claims.Nonce != nonce || (len(claims.Audience) > 1 && claims.AuthorizedParty != c.cfg.ClientID) || (claims.AuthorizedParty != "" && claims.AuthorizedParty != c.cfg.ClientID) {
		return nil, ErrProtocol
	}
	return claims, nil
}
