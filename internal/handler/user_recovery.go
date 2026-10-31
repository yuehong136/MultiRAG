package handler

import (
	"net/http"
	"net/url"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/service"
)

func (h *UserHandler) ForgotCaptcha(c *gin.Context) {
	c.Header("Cache-Control", "no-store")
	c.Header("X-Content-Type-Options", "nosniff")
	query, err := url.ParseQuery(c.Request.URL.RawQuery)
	if err != nil || len(query) != 1 || len(query["email"]) != 1 {
		jsonResponse(c, common.CodeArgumentError, false, "email is required")
		return
	}
	image, code, err := h.userService.ForgotIssueCaptcha(c.Request.Context(), query.Get("email"))
	if err != nil {
		jsonResponse(c, code, false, err.Error())
		return
	}
	c.Data(http.StatusOK, "image/png", image)
}

func (h *UserHandler) ForgotSendOTP(c *gin.Context) {
	c.Header("Cache-Control", "no-store")
	var req struct {
		Email   string `json:"email"`
		Captcha string `json:"captcha"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		jsonResponse(c, common.CodeArgumentError, false, "email and captcha required")
		return
	}
	code, err := h.userService.ForgotSendOTP(c.Request.Context(), req.Email, req.Captcha)
	if err != nil {
		jsonResponse(c, code, false, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, true, "verification passed, email sent")
}

func (h *UserHandler) ForgotVerifyOTP(c *gin.Context) {
	c.Header("Cache-Control", "no-store")
	var req struct {
		Email string `json:"email"`
		OTP   string `json:"otp"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		jsonResponse(c, common.CodeArgumentError, false, "email and otp are required")
		return
	}
	session, code, err := h.userService.ForgotVerifyOTP(c.Request.Context(), req.Email, req.OTP)
	if err != nil {
		jsonResponse(c, code, false, err.Error())
		return
	}
	setRecoveryCookie(c, session, 300)
	jsonResponse(c, common.CodeSuccess, true, "otp verified")
}

func (h *UserHandler) ForgotResetPassword(c *gin.Context) {
	c.Header("Cache-Control", "no-store")
	var req service.ForgotResetPasswordRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		jsonResponse(c, common.CodeArgumentError, false, "email and passwords are required")
		return
	}
	session, _ := c.Cookie(recoveryCookieName)
	code, err := h.userService.ForgotResetPassword(c.Request.Context(), req, session)
	if err != nil {
		jsonResponse(c, code, false, err.Error())
		return
	}
	setRecoveryCookie(c, "", -1)
	jsonResponse(c, common.CodeSuccess, true, "Password reset successful.")
}
