package service

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"net/url"
	"testing"
)

func canonicalGoogleCredentials() json.RawMessage {
	return json.RawMessage(`{"web":{"client_id":"owned-client","client_secret":"owned-client-secret","auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token"}}`)
}
func TestConnectorOAuthPKCEAndCredentialTypes(t *testing.T) {
	verifier, challenge, err := newPKCEChallenge()
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256([]byte(verifier))
	if len(verifier) < 43 || challenge != base64.RawURLEncoding.EncodeToString(sum[:]) {
		t.Fatal("PKCE challenge mismatch")
	}
	credentials, err := loadGoogleCredentials(canonicalGoogleCredentials())
	if err != nil {
		t.Fatal(err)
	}
	config, err := getGoogleWebClientConfig(credentials)
	if err != nil || config["web"] == nil {
		t.Fatal(err)
	}
	authorization, err := buildGoogleAuthorizationURL(googleOAuthAuthorizeURL, "owned-client", "https://app.invalid/callback", "flow", gmailOAuthScopes, challenge)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := url.Parse(authorization)
	if err != nil {
		t.Fatal(err)
	}
	query := parsed.Query()
	if query.Get("state") != "flow" || query.Get("code_challenge_method") != "S256" || query.Get("code_verifier") != "" || query.Get("code_challenge") != challenge {
		t.Fatal("authorization PKCE parameters lost")
	}
	for _, raw := range []json.RawMessage{json.RawMessage(`null`), json.RawMessage(`[]`), json.RawMessage(`{"web":false}`), json.RawMessage(`{"web":{"token_uri":"http://127.0.0.1/private"}}`), json.RawMessage(`{"web":{"auth_uri":"https://attacker.invalid/oauth"}}`)} {
		value, err := loadGoogleCredentials(raw)
		if err == nil {
			_, err = getGoogleWebClientConfig(value)
		}
		if err == nil {
			t.Error("untrusted credentials accepted")
		}
	}
}
func TestConnectorOAuthRejectsUntrustedConfigurationWithoutWrites(t *testing.T) {
	f, s := canonicalOAuthServices(t)
	for _, raw := range []json.RawMessage{json.RawMessage(`null`), json.RawMessage(`{"web":{"client_id":"c","token_uri":"http://127.0.0.1/private"}}`), json.RawMessage(`{"web":{"client_id":"c","auth_uri":"https://attacker.invalid/oauth"}}`)} {
		if result, _, err := s.StartGoogleWebOAuth("owner", "google-drive", &StartGoogleWebOAuthRequest{Credentials: raw}); err == nil || result != nil {
			t.Fatal("invalid OAuth configuration minted flow")
		}
	}
	if count := f.Redis.DBSize(t.Context()).Val(); count != 0 {
		t.Fatal("invalid OAuth configuration wrote Redis", count)
	}
}
