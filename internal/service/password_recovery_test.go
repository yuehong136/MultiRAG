package service

import (
	"bytes"
	"context"
	"image/png"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/testsupport"
	"multirag/internal/testsupport/scratch"
	"multirag/internal/utility"
)

func TestForgotPasswordScratch(t *testing.T) {
	f := scratch.New(t)
	smtp := testsupport.NewSMTP(t)
	host, port := smtp.HostPort()
	sender := common.SMTPConfig{MailServer: host, MailPort: port, MailFromAddress: "no-reply@example.test", TimeoutSeconds: 2}
	if err := f.DB.AutoMigrate(&entity.User{}); err != nil {
		t.Fatal(err)
	}
	p := f.Prefix
	email := p + "@example.test"
	active := "1"
	token := "existing-session"
	login := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
	oldHash, err := NewUserService().HashPassword("old password")
	if err != nil {
		t.Fatal(err)
	}
	if err = f.DB.Create(&entity.User{ID: p + "user", Email: email, Nickname: "recovery", Password: &oldHash, AccessToken: &token, LastLoginTime: &login, Status: &active, AccountKind: "local"}).Error; err != nil {
		t.Fatal(err)
	}
	s := NewPasswordRecoveryService(f.DB, f.Redis, sender)
	keys := recoveryKeys(email)
	captcha := func() string {
		t.Helper()
		image, code, err := s.IssueCaptcha(t.Context(), email)
		if err != nil || code != common.CodeSuccess {
			t.Fatal("captcha issuance failed", code, err)
		}
		decoded, err := png.Decode(bytes.NewReader(image))
		if err != nil || decoded.Bounds().Dx() != 300 || decoded.Bounds().Dy() != 120 {
			t.Fatal("raw raster contract")
		}
		text, err := f.Redis.Get(t.Context(), keys[0]).Result()
		if err != nil || len(text) != 4 {
			t.Fatal("email-keyed captcha not persisted")
		}
		ttl, err := f.Redis.PTTL(t.Context(), keys[0]).Result()
		if err != nil || ttl <= 0 || ttl > time.Minute {
			t.Fatal("captcha ttl")
		}
		return text
	}
	readPassword := func() string {
		t.Helper()
		var value string
		if err := f.Read.QueryRow("SELECT password FROM t_ai_users WHERE id=$1", p+"user").Scan(&value); err != nil {
			t.Fatal(err)
		}
		return value
	}
	text := captcha()
	if code, err := s.SendOTP(t.Context(), strings.ToUpper(email), strings.ToLower(text)); err != nil || code != common.CodeSuccess {
		t.Fatal("send OTP", code, err)
	}
	messages := smtp.Messages()
	if len(messages) != 1 {
		t.Fatal("controlled SMTP did not accept exactly one message")
	}
	matches := regexp.MustCompile(`reset code is: ([A-Z]{4})`).FindStringSubmatch(messages[0])
	if len(matches) != 2 {
		t.Fatal("SMTP body missing reset code")
	}
	otp := matches[1]
	stored, err := f.Redis.Get(t.Context(), keys[1]).Result()
	if err != nil {
		t.Fatal(err)
	}
	hash, salt, err := utility.DecodeOTPStorageValue(stored)
	if err != nil || hash != utility.HashOTPCode(otp, salt) {
		t.Fatal("stored OTP hash differs from delivered code")
	}
	if exists, err := f.Redis.Exists(t.Context(), keys[0], keys[6]).Result(); err != nil || exists != 0 {
		t.Fatal("captcha/pending state not consumed")
	}
	text = captcha()
	if code, err := s.SendOTP(t.Context(), email, text); err == nil || code != common.CodeNotEffective {
		t.Fatal("resend cooldown ignored")
	}
	if value, _ := f.Redis.Get(t.Context(), keys[1]).Result(); value != stored {
		t.Fatal("throttled send changed active code")
	}
	bad := "AAAA"
	if otp == bad {
		bad = "BBBB"
	}
	var wrongWG sync.WaitGroup
	for range 16 {
		wrongWG.Add(1)
		go func() { defer wrongWG.Done(); _, _ = s.VerifyOTP(t.Context(), email, bad) }()
	}
	wrongWG.Wait()
	attempts, err := f.Redis.Get(t.Context(), keys[2]).Int()
	if err != nil || attempts != utility.OTPAttemptLimit {
		t.Fatal("attempt increments were lost or exceeded lock cap")
	}
	if exists, _ := f.Redis.Exists(t.Context(), keys[4]).Result(); exists != 1 {
		t.Fatal("attempt lock missing")
	}
	text = captcha()
	if code, err := s.SendOTP(t.Context(), email, text); err == nil || code != common.CodeNotEffective {
		t.Fatal("new captcha lifted attempt lock")
	}
	if attempts, _ := f.Redis.Get(t.Context(), keys[2]).Int(); attempts != utility.OTPAttemptLimit {
		t.Fatal("resend reset locked attempts")
	}
	// Only this test's isolated key is removed to exercise the subsequent phase.
	if err = f.Redis.Del(t.Context(), keys[4]).Err(); err != nil {
		t.Fatal(err)
	}
	var correctWG sync.WaitGroup
	var verified atomic.Int32
	for range 8 {
		correctWG.Add(1)
		go func() {
			defer correctWG.Done()
			code, err := s.VerifyOTP(t.Context(), email, strings.ToLower(otp))
			if err == nil && code == common.CodeSuccess {
				verified.Add(1)
			}
		}()
	}
	correctWG.Wait()
	if verified.Load() != 1 {
		t.Fatal("OTP was consumed more than once")
	}
	if code, err := s.ResetPassword(t.Context(), ForgotResetPasswordRequest{Email: email, NewPassword: "new password", ConfirmNewPassword: "different"}); err == nil || code != common.CodeArgumentError {
		t.Fatal("mismatched password accepted")
	}
	var resetWG sync.WaitGroup
	var reset atomic.Int32
	req := ForgotResetPasswordRequest{Email: email, NewPassword: "new password", ConfirmNewPassword: "new password"}
	for range 2 {
		resetWG.Add(1)
		go func() {
			defer resetWG.Done()
			code, err := s.ResetPassword(t.Context(), req)
			if err == nil && code == common.CodeSuccess {
				reset.Add(1)
			}
		}()
	}
	resetWG.Wait()
	if reset.Load() != 1 {
		t.Fatal("verification proof reset more than once")
	}
	if !NewUserService().VerifyPassword(readPassword(), "new password") {
		t.Fatal("independent SQL password readback")
	}
	var actualToken string
	var actualLogin time.Time
	if err = f.Read.QueryRow("SELECT access_token,last_login_time FROM t_ai_users WHERE id=$1", p+"user").Scan(&actualToken, &actualLogin); err != nil || actualToken != token || !actualLogin.Equal(login) {
		t.Fatal("password reset changed login/session state")
	}
	if exists, _ := f.Redis.Exists(t.Context(), keys[5], keys[7]).Result(); exists != 0 {
		t.Fatal("verification/claim remained after reset")
	}
	// Real SQL failure restores the original proof with its remaining TTL.
	if err = f.Redis.Set(t.Context(), keys[5], "previous-proof", 90*time.Second).Err(); err != nil {
		t.Fatal(err)
	}
	beforeHash := readPassword()
	if err = f.DB.Exec(`CREATE FUNCTION reject_g_reset() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'controlled password failure'; END $$; CREATE TRIGGER reject_g_reset BEFORE UPDATE OF password ON t_ai_users FOR EACH ROW EXECUTE FUNCTION reject_g_reset()`).Error; err != nil {
		t.Fatal(err)
	}
	if code, err := s.ResetPassword(t.Context(), req); err == nil || code != common.CodeServerError {
		t.Fatal("SQL failure acknowledged success")
	}
	if readPassword() != beforeHash {
		t.Fatal("SQL failure changed password")
	}
	proof, _ := f.Redis.Get(t.Context(), keys[5]).Result()
	ttl, _ := f.Redis.PTTL(t.Context(), keys[5]).Result()
	if proof != "previous-proof" || ttl <= 0 || ttl > 90*time.Second {
		t.Fatal("verification proof compensation or ttl")
	}
	if err = f.DB.Exec("DROP TRIGGER reject_g_reset ON t_ai_users; DROP FUNCTION reject_g_reset()").Error; err != nil {
		t.Fatal(err)
	}
	// SMTP failure compensates exactly the prior OTP/attempt/cooldown/proof.
	priorSalt, _ := utility.GenerateOTPSalt()
	priorCode := utility.EncodeOTPStorageValue(utility.HashOTPCode("ABCD", priorSalt), priorSalt)
	for _, entry := range []struct {
		key, value string
		ttl        time.Duration
	}{{keys[1], priorCode, 80 * time.Second}, {keys[2], "2", 70 * time.Second}, {keys[3], strconv.FormatInt(time.Now().Add(-70*time.Second).Unix(), 10), 60 * time.Second}, {keys[5], "old-proof", 50 * time.Second}} {
		if err = f.Redis.Set(t.Context(), entry.key, entry.value, entry.ttl).Err(); err != nil {
			t.Fatal(err)
		}
	}
	prior := map[string]string{}
	ttls := map[string]time.Duration{}
	for _, key := range []string{keys[1], keys[2], keys[3], keys[5]} {
		prior[key], _ = f.Redis.Get(t.Context(), key).Result()
		ttls[key], _ = f.Redis.PTTL(t.Context(), key).Result()
	}
	smtp.RejectData(true)
	text = captcha()
	if code, err := s.SendOTP(t.Context(), email, text); err == nil || code != common.CodeServerError {
		t.Fatal("SMTP rejection acknowledged success")
	}
	for _, key := range []string{keys[1], keys[2], keys[3], keys[5]} {
		value, _ := f.Redis.Get(t.Context(), key).Result()
		ttl, _ := f.Redis.PTTL(t.Context(), key).Result()
		if value != prior[key] || ttl <= 0 || ttl > ttls[key] {
			t.Fatal("SMTP compensation changed prior value or extended ttl")
		}
	}
	// A late failure must not overwrite a newer transition.
	entered, release := smtp.PauseData()
	text = captcha()
	done := make(chan common.ErrorCode, 1)
	go func() { code, _ := s.SendOTP(t.Context(), email, text); done <- code }()
	select {
	case <-entered:
	case <-time.After(3 * time.Second):
		t.Fatal("SMTP boundary never entered")
	}
	for _, entry := range []struct{ key, value string }{{keys[1], "newer-code"}, {keys[2], "1"}, {keys[3], "newer-last"}, {keys[5], "newer-proof"}, {keys[6], "newer-nonce"}} {
		if err = f.Redis.Set(t.Context(), entry.key, entry.value, time.Minute).Err(); err != nil {
			t.Fatal(err)
		}
	}
	release()
	if code := <-done; code != common.CodeServerError {
		t.Fatal("late SMTP failure acknowledged")
	}
	if value, _ := f.Redis.Get(t.Context(), keys[1]).Result(); value != "newer-code" {
		t.Fatal("late rollback overwrote new OTP")
	}
	if value, _ := f.Redis.Get(t.Context(), keys[5]).Result(); value != "newer-proof" {
		t.Fatal("late rollback overwrote new proof")
	}
	if err = f.Redis.Del(t.Context(), keys...).Err(); err != nil {
		t.Fatal(err)
	}
	// Inaccessible Redis cannot issue a captcha or mutate SQL.
	f.Journal.Event("borrow-unavailable-redis-boundary", map[string]any{"address": "127.0.0.1:1"})
	unavailable := redis.NewClient(&redis.Options{Addr: "127.0.0.1:1", MaxRetries: -1, DialTimeout: 100 * time.Millisecond, ReadTimeout: 100 * time.Millisecond, WriteTimeout: 100 * time.Millisecond})
	defer unavailable.Close()
	blocked := NewPasswordRecoveryService(f.DB, unavailable, sender)
	if _, code, err := blocked.IssueCaptcha(t.Context(), email); err == nil || code != common.CodeServerError {
		t.Fatal("unavailable Redis issued captcha")
	}
	if code, err := blocked.ResetPassword(t.Context(), req); err == nil || code != common.CodeServerError {
		t.Fatal("unavailable Redis reset password")
	}
	if readPassword() != beforeHash {
		t.Fatal("Redis failure changed SQL")
	}
	for i, kind := range []string{"external", "hybrid"} {
		id := p + strconv.Itoa(i)
		address := id + "@example.test"
		if err = f.DB.Create(&entity.User{ID: id, Email: address, Nickname: kind, AccountKind: kind, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		_, code, err := s.IssueCaptcha(t.Context(), address)
		if kind == "external" {
			if err == nil || code != common.CodeDataError {
				t.Fatal("external-only account entered recovery")
			}
		} else if err != nil || code != common.CodeSuccess {
			t.Fatal("hybrid account recovery unavailable")
		}
	}
}

func TestForgotSMTPDeadlineAndCancellation(t *testing.T) {
	smtp := testsupport.NewSMTP(t)
	host, port := smtp.HostPort()
	cfg := common.SMTPConfig{MailServer: host, MailPort: port, MailFromAddress: "no-reply@example.test", TimeoutSeconds: 1}
	entered, release := smtp.PauseData()
	defer release()
	done := make(chan error, 1)
	started := time.Now()
	go func() { done <- utility.SendResetCodeEmail(t.Context(), cfg, "reader@example.test", "ABCD", 5) }()
	select {
	case <-entered:
	case <-time.After(2 * time.Second):
		t.Fatal("SMTP body was not sent")
	}
	select {
	case err := <-done:
		if err == nil || time.Since(started) > 2500*time.Millisecond {
			t.Fatal("SMTP deadline not enforced")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("SMTP hung beyond deadline")
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if err := utility.SendResetCodeEmail(ctx, cfg, "reader@example.test", "ABCD", 5); err == nil {
		t.Fatal("canceled SMTP succeeded")
	}
}
