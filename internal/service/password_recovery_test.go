package service

import (
	"bytes"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"image/png"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
	"multirag/internal/common"
	"multirag/internal/dao"
	rediscache "multirag/internal/engine/redis"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/testsupport"
	"multirag/internal/testsupport/scratch"
	"multirag/internal/utility"
)

func canonicalRecoveryFixture(t *testing.T) (*scratch.Services, *UserService, *testsupport.SMTPServer, string, func(string) string) {
	t.Helper()
	f := scratch.New(t)
	oldDB := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = oldDB })
	if rediscache.Get() != nil {
		t.Fatal("unexpected initialized Redis fixture")
	}
	if err := rediscache.Init(&f.Config.Redis); err != nil {
		t.Fatal("initialize owned Redis")
	}
	t.Cleanup(func() {
		if err := rediscache.Close(); err != nil {
			t.Error(err)
		}
	})
	if err := f.DB.AutoMigrate(&entity.User{}); err != nil {
		t.Fatal(err)
	}
	smtp := testsupport.NewSMTP(t)
	host, port := smtp.HostPort()
	directory := filepath.Join(os.TempDir(), "canonical-recovery-"+uuid.NewString())
	f.Journal.Event("reserve-recovery-key-directory", map[string]any{"path": directory})
	if err := os.MkdirAll(filepath.Join(directory, "conf"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(directory); err != nil {
			t.Error(err)
		}
		_, err := os.Lstat(directory)
		f.Journal.Event("cleanup-recovery-key-directory", map[string]any{"path": directory, "absent": os.IsNotExist(err)})
		if !os.IsNotExist(err) {
			t.Error("recovery fixture remains")
		}
	})
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(filepath.Join(directory, "conf/private.pem"), pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)}), 0600); err != nil {
		t.Fatal(err)
	}
	t.Chdir(directory)
	if server.GetConfig() == nil {
		configuration := filepath.Join(directory, "conf/service.json")
		if err := os.WriteFile(configuration, []byte(`{}`), 0600); err != nil {
			t.Fatal(err)
		}
		if err := server.FromConfigFile(configuration); err != nil {
			t.Fatal(err)
		}
	}
	cfg := server.GetConfig()
	oldSMTP := cfg.SMTP
	cfg.SMTP = common.SMTPConfig{MailServer: host, MailPort: port, MailFromAddress: "no-reply@example.test"}
	t.Cleanup(func() { cfg.SMTP = oldSMTP })

	encrypt := func(value string) string {
		data, err := rsa.EncryptPKCS1v15(rand.Reader, &key.PublicKey, []byte(value))
		if err != nil {
			t.Fatal(err)
		}
		return base64.StdEncoding.EncodeToString(data)
	}
	svc := NewUserService()
	hashed, err := svc.HashPassword("old-password")
	if err != nil {
		t.Fatal(err)
	}
	email := f.Prefix + "@example.test"
	active := "1"
	token := "old-session"
	if err = f.DB.Create(&entity.User{ID: f.Prefix + "user", Email: email, Nickname: "Recovery", Password: &hashed, AccessToken: &token, IsActive: "1", Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	return f, svc, smtp, email, encrypt
}

func TestForgotPasswordScratch(t *testing.T) {
	f, s, smtp, email, encrypt := canonicalRecoveryFixture(t)
	id, image, code, err := s.ForgotIssueCaptcha(email)
	if err != nil || code != common.CodeSuccess || id == "" {
		t.Fatal("captcha issuance", code, err)
	}
	encoded := strings.TrimPrefix(image, "data:image/png;base64,")
	raw, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = png.Decode(bytes.NewReader(raw)); err != nil {
		t.Fatal("captcha must be real PNG", err)
	}
	answer, err := f.Redis.Get(t.Context(), utility.CaptchaIDRedisKey(id)).Result()
	if err != nil || answer == "" {
		t.Fatal("captcha not persisted")
	}
	if ttl := f.Redis.TTL(t.Context(), utility.CaptchaIDRedisKey(id)).Val(); ttl <= 0 || ttl > time.Minute {
		t.Fatal("captcha TTL")
	}
	if code, err = s.ForgotSendOTP(t.Context(), email, id, strings.ToLower(answer)); err != nil || code != common.CodeSuccess {
		t.Fatal("send OTP", code, err)
	}
	if len(smtp.Messages()) != 1 {
		t.Fatal("SMTP delivery count", len(smtp.Messages()))
	}
	match := regexp.MustCompile(`reset code is: ([A-Z]{4})`).FindStringSubmatch(smtp.Messages()[0])
	if len(match) != 2 {
		t.Fatal("OTP not in actual accepted SMTP message")
	}
	otp := match[1]
	if _, err = s.ForgotSendOTP(t.Context(), email, id, answer); err == nil || len(smtp.Messages()) != 1 {
		t.Fatal("reused captcha delivered another OTP")
	}
	key, attempts, _, lock := utility.OTPRedisKeys(email)
	stored := f.Redis.Get(t.Context(), key).Val()
	hash, salt, err := utility.DecodeOTPStorageValue(stored)
	if err != nil || hash != utility.HashOTPCode(otp, salt) {
		t.Fatal("stored OTP differs from delivered code")
	}
	bad := "AAAA"
	if otp == bad {
		bad = "BBBB"
	}
	var wg sync.WaitGroup
	for range 16 {
		wg.Add(1)
		go func() { defer wg.Done(); _, _ = s.ForgotVerifyOTP(email, bad) }()
	}
	wg.Wait()
	if n := f.Redis.Get(t.Context(), attempts).Val(); n != "5" || f.Redis.Exists(t.Context(), lock).Val() != 1 {
		t.Fatal("concurrent attempts were lost or lockout absent", n)
	}
	if _, err = s.ForgotVerifyOTP(email, otp); err == nil {
		t.Fatal("locked email verified")
	}
	f.Redis.Del(t.Context(), lock, attempts)
	if code, err = s.ForgotVerifyOTP(email, strings.ToLower(otp)); err != nil || code != common.CodeSuccess {
		t.Fatal("verify delivered OTP", code, err)
	}
	if f.Redis.Exists(t.Context(), key).Val() != 0 {
		t.Fatal("verified OTP remains reusable")
	}
	if _, err = s.ForgotVerifyOTP(email, otp); err == nil {
		t.Fatal("OTP replay succeeded")
	}
	request := &ForgotResetPasswordRequest{Email: email, NewPassword: encrypt("new-password"), ConfirmNewPassword: encrypt("new-password")}
	var successes atomic.Int32
	for range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			user, code, err := s.ForgotResetPassword(request)
			if err == nil && code == 0 && user != nil {
				successes.Add(1)
			}
		}()
	}
	wg.Wait()
	if successes.Load() != 1 {
		t.Fatal("verification proof reset more than once", successes.Load())
	}
	var password, token string
	if err = f.Read.QueryRow("SELECT password,access_token FROM user WHERE email=?", email).Scan(&password, &token); err != nil || !s.VerifyPassword(password, "new-password") || token == "old-session" || token == "" {
		t.Fatal("independent persisted password/token readback", err)
	}
	if f.Redis.Exists(t.Context(), utility.OTPVerifiedRedisKey(email)).Val() != 0 {
		t.Fatal("reset proof survived")
	}
	f.Journal.Event("canonical-recovery-readback", map[string]any{"smtp_messages": 1, "reset_successes": successes.Load(), "rotated_session": true})
}

func TestForgotSMTPFailureRestoresDeliveryState(t *testing.T) {
	f, s, smtp, email, _ := canonicalRecoveryFixture(t)
	smtp.RejectData(true)
	id, _, _, err := s.ForgotIssueCaptcha(email)
	if err != nil {
		t.Fatal(err)
	}
	answer := f.Redis.Get(t.Context(), utility.CaptchaIDRedisKey(id)).Val()
	if _, err = s.ForgotSendOTP(t.Context(), email, id, answer); err == nil {
		t.Fatal("rejected SMTP accepted")
	}
	code, attempts, sent, _ := utility.OTPRedisKeys(email)
	if n := f.Redis.Exists(t.Context(), code, attempts, sent, sent+":sending").Val(); n != 0 {
		t.Fatal("failed delivery left OTP/cooldown", n)
	}
	if len(smtp.Messages()) != 0 {
		t.Fatal("rejected SMTP message counted as accepted")
	}
}
