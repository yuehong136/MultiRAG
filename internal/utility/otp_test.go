package utility

import (
	"bytes"
	"image/png"
	"strings"
	"testing"
)

func TestRecoveryOTPAndCaptchaContracts(t *testing.T) {
	code, err := GenerateOTPCode()
	if err != nil || len(code) != 4 || strings.Trim(code, "ABCDEFGHIJKLMNOPQRSTUVWXYZ") != "" {
		t.Fatal("OTP generation")
	}
	salt, err := GenerateOTPSalt()
	if err != nil {
		t.Fatal(err)
	}
	stored := EncodeOTPStorageValue(HashOTPCode(code, salt), salt)
	hash, restored, err := DecodeOTPStorageValue(stored)
	if err != nil || hash != HashOTPCode(code, restored) || !bytes.Equal(salt, restored) {
		t.Fatal("salted OTP roundtrip")
	}
	for _, value := range []string{"", ":", stored + ":tail", "xyz:deadbeef", strings.Repeat("a", 64) + ":"} {
		if _, _, err := DecodeOTPStorageValue(value); err == nil {
			t.Fatal("malformed OTP accepted")
		}
	}
	if CaptchaRedisKey(" A@Example.Test ") != "captcha:a@example.test" {
		t.Fatal("captcha is not keyed by normalized email")
	}
	image, err := RenderCaptchaPNG("AB12")
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := png.Decode(bytes.NewReader(image))
	if err != nil || decoded.Bounds().Dx() != 300 || decoded.Bounds().Dy() != 120 {
		t.Fatal("captcha is not a raw raster")
	}
}
