package handler

import (
	"github.com/gin-gonic/gin"
	"multirag/internal/cache"
	"multirag/internal/service"
	"net/http"
	"net/url"
)

const oauthStateCookie = "multirag_oauth_state"

func setOAuthStateCookie(c *gin.Context, state string, maxAge int) {
	http.SetCookie(c.Writer, &http.Cookie{Name: oauthStateCookie, Value: state, Path: "/api/v1/auth/", MaxAge: maxAge, HttpOnly: true, Secure: true, SameSite: http.SameSiteLaxMode})
}
func oauthErrorRedirect(c *gin.Context, err error) {
	code := "server_error"
	switch err {
	case service.ErrOAuthInvalidChannel, service.ErrOAuthInvalidState, service.ErrOAuthUnavailable, service.ErrOAuthMissingCode, service.ErrOAuthProtocol, service.ErrOAuthIdentityBindingRequired:
		code = err.Error()
	}
	c.Redirect(http.StatusFound, "/?"+url.Values{"error": {code}}.Encode())
}

// OAuthLogin starts protocol verification; platform account login is disabled.
func (h *UserHandler) OAuthLogin(c *gin.Context) {
	result, err := h.userService.OAuthLoginInitiate(c.Request.Context(), c.Param("channel"), cache.Get())
	if err != nil {
		oauthErrorRedirect(c, err)
		return
	}
	setOAuthStateCookie(c, result.State, 300)
	c.Redirect(http.StatusFound, result.AuthURL)
}
func (h *UserHandler) OAuthCallback(c *gin.Context) {
	cookie, _ := c.Cookie(oauthStateCookie)
	setOAuthStateCookie(c, "", -1)
	query, err := url.ParseQuery(c.Request.URL.RawQuery)
	if err != nil || len(query["state"]) != 1 || len(query["code"]) > 1 || len(query["error"]) > 0 {
		oauthErrorRedirect(c, service.ErrOAuthInvalidState)
		return
	}
	oauthErrorRedirect(c, h.userService.OAuthCallback(c.Request.Context(), c.Param("channel"), query.Get("code"), query.Get("state"), cookie, cache.Get()))
}
