# OAuth protocol verification in the Go API

The Go API is a parallel implementation; the production backend uses Python.
These new endpoints have controlled HTTP acceptance coverage; no active Web login
UI caller or real-provider/browser login has been verified.

The Go API can start and verify an authorization-code exchange through
`GET /api/v1/auth/login/{channel}` and
`GET /api/v1/auth/oauth/{channel}/callback`.

**Platform account login is disabled.** A valid provider exchange ends with
`/?error=oauth_identity_binding_required`. The callback never links an email,
registers an account or tenant, changes an existing session, or issues a platform
token. Persisted provider-subject identity binding is required before enabling
account login. These routes do not implement password reset.

Channels use server-controlled `oauth.<channel>` configuration: `type`
(`oauth2`, `oidc`, or `github`), `client_id`, `client_secret`, `redirect_uri`,
`scope`, and provider endpoints `authorization_url`, `token_url`, `userinfo_url`.
OIDC uses `issuer` and validates its discovery document before using its
endpoints. GitHub uses its fixed authorization, token, and user endpoints; it
reads the numeric provider ID and does not request email permissions.

All configured endpoints and the redirect URI require HTTPS. Requests are
bounded to seven seconds and one MiB, use request cancellation, reject HTTP
redirects, and return fixed public errors without provider bodies or tokens.
Redis is mandatory. State lasts five minutes, is tied to a Secure, HttpOnly,
SameSite=Lax browser cookie and channel, and is consumed atomically with
`GETDEL` before provider exchange. Each attempt uses S256 PKCE. OIDC additionally
requires an RS256 ID token with a matching JWKS key, issuer, audience, expiry,
issued-at, nonce, subject, and authorized party where applicable; userinfo must
report the same subject. Other signing algorithms are currently refused.

`client_test.go` covers protocol acceptance and rejection without external
network access. `router.TestAfter1eGMCPAndOAuth` uses registered scratch PostgreSQL,
a separately reserved empty Redis database, real registered HTTP routes and a
controlled TLS provider, and independently reads SQL state after refusals.
