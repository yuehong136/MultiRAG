package handler

import (
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/server"
)

const recoveryCookieName = "multirag_recovery"
const recoveryCookiePath = "/api/v1/auth/password"

func recoveryOrigin(value string) (*url.URL, bool) {
	u, err := url.Parse(value)
	if err != nil || u.Host == "" || u.User != nil || u.Path != "" || u.RawQuery != "" || u.Fragment != "" || (u.Scheme != "http" && u.Scheme != "https") {
		return nil, false
	}
	return u, true
}

// PasswordRecoveryOriginMiddleware protects only the public recovery routes.
// Browser cross-origin use needs an exact allowed origin and a same-site cookie;
// cross-site use is intentionally unsupported. Forwarded headers grant no trust.
func PasswordRecoveryOriginMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		if !strings.HasPrefix(c.Request.URL.Path, recoveryCookiePath+"/") {
			c.Next()
			return
		}
		c.Header("Cache-Control", "no-store")
		c.Header("X-Content-Type-Options", "nosniff")
		origin := c.GetHeader("Origin")
		site := c.GetHeader("Sec-Fetch-Site")
		allowed := site != "cross-site"
		if origin == "" {
			allowed = allowed && (site == "" || site == "none" || site == "same-origin")
		} else {
			u, valid := recoveryOrigin(origin)
			scheme := "http"
			if c.Request.TLS != nil {
				scheme = "https"
			}
			accepted := valid && u.Scheme == scheme && strings.EqualFold(u.Host, c.Request.Host)
			if !accepted && valid {
				if cfg := server.GetConfig(); cfg != nil {
					for _, candidate := range cfg.Authentication.PasswordRecoveryOrigins {
						if candidate == origin {
							accepted = true
							break
						}
					}
				}
			}
			allowed = allowed && accepted
		}
		if !allowed {
			c.AbortWithStatusJSON(http.StatusForbidden, gin.H{"code": common.CodeForbidden, "data": false, "message": "recovery origin is not allowed"})
			return
		}
		if origin != "" {
			c.Header("Access-Control-Allow-Origin", origin)
			c.Header("Access-Control-Allow-Credentials", "true")
			c.Writer.Header().Add("Vary", "Origin")
		}
		if c.Request.Method == http.MethodOptions {
			if c.GetHeader("Access-Control-Request-Method") != http.MethodPost {
				c.AbortWithStatus(http.StatusForbidden)
				return
			}
			for _, header := range strings.Split(c.GetHeader("Access-Control-Request-Headers"), ",") {
				if name := strings.TrimSpace(header); name != "" && !strings.EqualFold(name, "Content-Type") {
					c.AbortWithStatus(http.StatusForbidden)
					return
				}
			}
			c.Header("Access-Control-Allow-Methods", "POST")
			c.Header("Access-Control-Allow-Headers", "Content-Type")
			c.AbortWithStatus(http.StatusNoContent)
			return
		}
		c.Next()
	}
}

func setRecoveryCookie(c *gin.Context, value string, maxAge int) {
	host := c.Request.URL.Hostname()
	if host == "" {
		host = c.Request.Host
		if name, _, err := net.SplitHostPort(host); err == nil {
			host = name
		}
	}
	ip := net.ParseIP(host)
	loopback := strings.EqualFold(host, "localhost") || ip != nil && ip.IsLoopback()
	secure := c.Request.TLS != nil || !loopback
	if origin, valid := recoveryOrigin(c.GetHeader("Origin")); valid && origin.Scheme == "https" {
		secure = true
	}
	cookie := &http.Cookie{Name: recoveryCookieName, Value: value, Path: recoveryCookiePath, MaxAge: maxAge, HttpOnly: true, Secure: secure, SameSite: http.SameSiteStrictMode}
	if maxAge < 0 {
		cookie.Expires = time.Unix(1, 0).UTC()
	}
	http.SetCookie(c.Writer, cookie)
}
