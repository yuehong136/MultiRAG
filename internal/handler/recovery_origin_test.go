package handler

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
)

func TestRecoveryOriginBeforeEffects(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, test := range []struct {
		name, method, origin, site, requestedMethod, requestedHeaders string
		want                                                          int
	}{
		{"same origin", "POST", "http://localhost:8000", "same-origin", "", "", 200},
		{"CLI", "POST", "", "", "", "", 200},
		{"hostile", "POST", "https://hostile.example", "cross-site", "", "", 403},
		{"opaque", "POST", "null", "", "", "", 403},
		{"path", "POST", "http://localhost:8000/", "", "", "", 403},
		{"userinfo", "POST", "http://user@localhost:8000", "", "", "", 403},
		{"missing browser origin", "POST", "", "same-site", "", "", 403},
		{"unlisted port", "POST", "http://localhost:5173", "same-site", "", "", 403},
		{"preflight", "OPTIONS", "http://localhost:8000", "same-origin", "POST", "content-type", 204},
		{"preflight method", "OPTIONS", "http://localhost:8000", "same-origin", "DELETE", "", 403},
		{"preflight headers", "OPTIONS", "http://localhost:8000", "same-origin", "POST", "X-Hostile", 403},
	} {
		t.Run(test.name, func(t *testing.T) {
			effects := 0
			r := gin.New()
			r.Use(PasswordRecoveryOriginMiddleware())
			r.POST(recoveryCookiePath+"/reset", func(c *gin.Context) { effects++; c.Status(200) })
			request := httptest.NewRequest(test.method, "http://localhost:8000"+recoveryCookiePath+"/reset", nil)
			request.Header.Set("Origin", test.origin)
			request.Header.Set("Sec-Fetch-Site", test.site)
			request.Header.Set("Access-Control-Request-Method", test.requestedMethod)
			request.Header.Set("Access-Control-Request-Headers", test.requestedHeaders)
			w := httptest.NewRecorder()
			r.ServeHTTP(w, request)
			if w.Code != test.want || effects != map[bool]int{true: 1, false: 0}[test.want == 200] {
				t.Fatal("recovery origin gate", w.Code, effects)
			}
			if test.want < 300 && test.origin != "" && (w.Header().Get("Access-Control-Allow-Origin") != test.origin || w.Header().Get("Access-Control-Allow-Credentials") != "true") {
				t.Fatal("credentialed CORS contract")
			}
		})
	}
}

func TestRecoveryCookiePolicy(t *testing.T) {
	for _, address := range []string{"http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000", "https://api.example.test", "http://api.example.test"} {
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		c.Request = httptest.NewRequest("POST", address+recoveryCookiePath+"/forgot/otp/verify", nil)
		setRecoveryCookie(c, strings.Repeat("a", 64), 300)
		cookies := w.Result().Cookies()
		if len(cookies) != 1 {
			t.Fatal("recovery cookie count")
		}
		cookie := cookies[0]
		wantSecure := strings.Contains(address, "api.example.test")
		if cookie.Secure != wantSecure || !cookie.HttpOnly || cookie.SameSite != http.SameSiteStrictMode || cookie.Path != recoveryCookiePath || cookie.Domain != "" || cookie.MaxAge != 300 {
			t.Fatal("recovery cookie policy", address)
		}
	}
}
