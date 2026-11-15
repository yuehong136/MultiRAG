# Go OAuth clients

`client.go`, `oidc.go` and `github.go` keep the corresponding OAuth client
implementations. `NewClient` selects OAuth2, OIDC or GitHub from server-controlled
configuration. Production continues to use the Python backend.

`AuthorizationURL` builds the authorization-code redirect. `ExchangeCodeForToken`
and `FetchUserInfo` use the caller's context, bounded responses, checked HTTP
status and fixed errors that omit provider bodies and credentials. Redirects on
token and user-info requests are refused.

OIDC resolves missing endpoints from the issuer's discovery document. The current
implementation uses authenticated userinfo and does not verify or parse ID-token
claims; it does not provide a separate JWKS verifier, nonce contract or PKCE
facade. Its discovery loader has its own bounded timeout.

GitHub uses its fixed authorization, token and user endpoints, defaults to the
`user:email` scope, and reads `/user/emails`. It selects a verified primary email,
then a verified fallback. Errors from either user request propagate.

Account creation, session persistence, browser state and connector OAuth are
owned by the original service/handler functions, outside this utility package.
`client_test.go` exercises the client request and error contracts with controlled
transports. Actual account and connector flows use their own SQL/HTTP/Redis
acceptance tests. These tests do not establish real provider or browser login
acceptance.
