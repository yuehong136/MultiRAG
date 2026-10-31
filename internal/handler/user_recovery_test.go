package handler

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/service"
)

func TestRecoveryHandlerRejectsMalformedInput(t *testing.T) {
	gin.SetMode(gin.TestMode)
	h := NewUserHandler(service.NewUserService())
	for _, test := range []struct {
		path, body string
		method     func(*gin.Context)
	}{{"/captcha?email=a&email=b", "", h.ForgotCaptcha}, {"/captcha?email=%xx", "", h.ForgotCaptcha}, {"/otp", `{"email":1}`, h.ForgotSendOTP}, {"/verify", `{"email":`, h.ForgotVerifyOTP}, {"/reset", `[]`, h.ForgotResetPassword}} {
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		c.Request = httptest.NewRequest("POST", test.path, strings.NewReader(test.body))
		c.Request.Header.Set("Content-Type", "application/json")
		test.method(c)
		var result struct {
			Code common.ErrorCode `json:"code"`
			Data any              `json:"data"`
		}
		if json.Unmarshal(w.Body.Bytes(), &result) != nil || result.Code != common.CodeArgumentError || result.Data != false {
			t.Fatal("malformed recovery input accepted", w.Code)
		}
		if w.Header().Get("Authorization") != "" || w.Header().Get("Set-Cookie") != "" {
			t.Fatal("recovery endpoint issued a session")
		}
	}
}
