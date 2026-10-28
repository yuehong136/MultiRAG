package service

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"time"

	"multirag/internal/cache"
	"multirag/internal/server"
	"multirag/internal/utility/oauth"
)

var (
	ErrOAuthInvalidChannel          = errors.New("invalid_channel")
	ErrOAuthInvalidState            = errors.New("invalid_state")
	ErrOAuthUnavailable             = errors.New("oauth_unavailable")
	ErrOAuthMissingCode             = errors.New("missing_code")
	ErrOAuthProtocol                = errors.New("oauth_protocol_error")
	ErrOAuthIdentityBindingRequired = errors.New("oauth_identity_binding_required")
)

const oauthStateTTL = 5 * time.Minute
const oauthStatePrefix = "oauth:state:"

type oauthAttempt struct{ Channel, Verifier, Nonce string }
type OAuthLoginInit struct{ State, AuthURL string }

func oauthConfig(channel string) (oauth.Config, error) {
	cfg := server.GetConfig()
	if cfg == nil {
		return oauth.Config{}, ErrOAuthInvalidChannel
	}
	provider, ok := cfg.OAuth[channel]
	if !ok || channel == "" {
		return oauth.Config{}, ErrOAuthInvalidChannel
	}
	return oauth.Config{Type: provider.Type, ClientID: provider.ClientID, ClientSecret: provider.ClientSecret, AuthorizationURL: provider.AuthorizationURL, TokenURL: provider.TokenURL, UserinfoURL: provider.UserinfoURL, RedirectURI: provider.RedirectURI, Scope: provider.Scope, Issuer: provider.Issuer}, nil
}
func oauthRandom() (string, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", ErrOAuthUnavailable
	}
	return hex.EncodeToString(raw), nil
}

func (s *UserService) OAuthLoginInitiate(ctx context.Context, channel string, store *cache.RedisClient) (*OAuthLoginInit, error) {
	cfg, err := oauthConfig(channel)
	if err != nil {
		return nil, err
	}
	if store == nil || store.GetClient() == nil {
		return nil, ErrOAuthUnavailable
	}
	client, err := oauth.NewClient(ctx, cfg)
	if err != nil {
		return nil, ErrOAuthProtocol
	}
	state, err := oauthRandom()
	if err != nil {
		return nil, err
	}
	verifier, err := oauthRandom()
	if err != nil {
		return nil, err
	}
	nonce, err := oauthRandom()
	if err != nil {
		return nil, err
	}
	authURL, err := client.AuthorizationURL(state, verifier, nonce)
	if err != nil {
		return nil, ErrOAuthProtocol
	}
	raw, _ := json.Marshal(oauthAttempt{Channel: channel, Verifier: verifier, Nonce: nonce})
	if ok, err := store.GetClient().SetNX(ctx, oauthStatePrefix+state, string(raw), oauthStateTTL).Result(); err != nil || !ok {
		return nil, ErrOAuthUnavailable
	}
	return &OAuthLoginInit{State: state, AuthURL: authURL}, nil
}

// OAuthCallback consumes state atomically before provider I/O. No account,
// tenant or token is created: provider-subject binding is not available yet.
func (s *UserService) OAuthCallback(ctx context.Context, channel, code, state, cookie string, store *cache.RedisClient) error {
	cfg, err := oauthConfig(channel)
	if err != nil {
		return err
	}
	if store == nil || store.GetClient() == nil {
		return ErrOAuthUnavailable
	}
	if len(state) != 64 || len(cookie) != 64 || subtle.ConstantTimeCompare([]byte(state), []byte(cookie)) != 1 {
		return ErrOAuthInvalidState
	}
	raw, err := store.GetClient().GetDel(ctx, oauthStatePrefix+state).Result()
	if err != nil {
		return ErrOAuthInvalidState
	}
	var attempt oauthAttempt
	if json.Unmarshal([]byte(raw), &attempt) != nil || attempt.Channel != channel || len(attempt.Verifier) != 64 || len(attempt.Nonce) != 64 {
		return ErrOAuthInvalidState
	}
	if code == "" {
		return ErrOAuthMissingCode
	}
	client, err := oauth.NewClient(ctx, cfg)
	if err != nil {
		return ErrOAuthProtocol
	}
	if _, err := client.VerifyCode(ctx, code, attempt.Verifier, attempt.Nonce); err != nil {
		return ErrOAuthProtocol
	}
	return ErrOAuthIdentityBindingRequired
}
