package handler

import (
	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/service"
	"net/http"
)

func (h *ConnectorHandler) StartGoogleWebOAuth(c *gin.Context) {
	c.Header("Cache-Control", "no-store")
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	var request service.StartGoogleWebOAuthRequest
	if bindObjectRequest(c, &request) != nil {
		jsonError(c, common.CodeBadRequest, "Invalid OAuth request")
		return
	}
	source, valid := connectorOAuthSourceQuery(c)
	if !valid {
		jsonError(c, common.CodeBadRequest, "One Google source is required")
		return
	}
	result, code, err := h.connectorService.StartGoogleWebOAuth(c.Request.Context(), user.ID, source, &request)
	if err != nil {
		jsonError(c, code, "Unable to start connector authorization")
		return
	}
	jsonResponse(c, code, result, "success")
}

func (h *ConnectorHandler) PollGoogleWebOAuthResult(c *gin.Context) {
	c.Header("Cache-Control", "no-store")
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	var request service.PollGoogleWebOAuthResultRequest
	if bindObjectRequest(c, &request) != nil {
		jsonError(c, common.CodeBadRequest, "Invalid OAuth result request")
		return
	}
	source, valid := connectorOAuthSourceQuery(c)
	if !valid {
		jsonError(c, common.CodeBadRequest, "One Google source is required")
		return
	}
	result, code, err := h.connectorService.PollGoogleWebOAuthResult(c.Request.Context(), user.ID, source, &request)
	if err != nil {
		jsonError(c, code, "Unable to read connector authorization")
		return
	}
	jsonResponse(c, code, result, "success")
}

func (h *ConnectorHandler) GoogleDriveWebOAuthCallback(c *gin.Context) {
	h.googleWebOAuthCallback(c, "google-drive")
}
func (h *ConnectorHandler) GmailWebOAuthCallback(c *gin.Context) {
	h.googleWebOAuthCallback(c, "gmail")
}
func (h *ConnectorHandler) googleWebOAuthCallback(c *gin.Context, source string) {
	c.Header("Cache-Control", "no-store")
	c.Header("X-Content-Type-Options", "nosniff")
	c.Header("Referrer-Policy", "no-referrer")
	c.Header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'")
	state, oauthError, code := c.Query("state"), c.Query("error"), c.Query("code")
	for _, key := range []string{"state", "error", "code"} {
		if len(c.Request.URL.Query()[key]) > 1 {
			state = ""
			break
		}
	}
	html := h.connectorService.GoogleWebOAuthCallback(c.Request.Context(), source, state, oauthError, code)
	c.Data(http.StatusOK, "text/html; charset=utf-8", []byte(html))
}

func connectorOAuthSourceQuery(c *gin.Context) (string, bool) {
	sources := c.Request.URL.Query()["source"]
	if len(sources) != 1 || sources[0] != "google-drive" && sources[0] != "gmail" {
		return "", false
	}
	return sources[0], true
}
