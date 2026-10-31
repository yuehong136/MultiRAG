package service

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"net/mail"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/cache"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/utility"
)

// PasswordRecoveryService uses raw Redis errors and atomic transitions. An
// unavailable state store cannot authorize a reset or silently drop an attempt.
type PasswordRecoveryService struct {
	db           *gorm.DB
	redis        *redis.Client
	smtp         common.SMTPConfig
	hashPassword func(string) (string, error)
}

func NewPasswordRecoveryService(db *gorm.DB, client *redis.Client, smtp common.SMTPConfig) *PasswordRecoveryService {
	return &PasswordRecoveryService{db: db, redis: client, smtp: smtp, hashPassword: NewUserService().HashPassword}
}

func (s *UserService) SetPasswordRecoveryService(recovery *PasswordRecoveryService) {
	s.recovery = recovery
}

func (s *UserService) passwordRecovery() *PasswordRecoveryService {
	if s.recovery != nil {
		return s.recovery
	}
	var client *redis.Client
	if cache.Get() != nil {
		client = cache.Get().GetClient()
	}
	var smtp common.SMTPConfig
	if cfg := server.GetConfig(); cfg != nil {
		smtp = cfg.SMTP
	}
	return NewPasswordRecoveryService(dao.DB, client, smtp)
}

func recoveryEmail(email string) (string, error) {
	email = strings.ToLower(strings.TrimSpace(email))
	address, err := mail.ParseAddress(email)
	if err != nil || address.Address != email || len(email) > 254 || strings.ContainsAny(email, "\r\n") {
		return "", fmt.Errorf("invalid email")
	}
	return email, nil
}

func (s *PasswordRecoveryService) user(ctx context.Context, db *gorm.DB, email string, lock bool) (*entity.User, error) {
	if db == nil {
		return nil, fmt.Errorf("recovery SQL unavailable")
	}
	query := db.WithContext(ctx).Where("LOWER(email)=? AND status='1' AND is_active=true AND account_kind IN ?", email, []string{"local", "hybrid"})
	if lock {
		query = query.Clauses(clause.Locking{Strength: "UPDATE"})
	}
	var rows []entity.User
	if err := query.Limit(2).Find(&rows).Error; err != nil {
		return nil, err
	}
	if len(rows) != 1 {
		return nil, gorm.ErrRecordNotFound
	}
	return &rows[0], nil
}

func (s *PasswordRecoveryService) ready(ctx context.Context, email string) (string, common.ErrorCode, error) {
	normalized, err := recoveryEmail(email)
	if err != nil {
		return "", common.CodeArgumentError, err
	}
	if cfg := server.GetConfig(); cfg != nil && cfg.Authentication.DisablePasswordLogin {
		return "", common.CodeForbidden, fmt.Errorf("password login is disabled")
	}
	if _, err = s.user(ctx, s.db, normalized, false); err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return "", common.CodeDataError, fmt.Errorf("invalid email")
		}
		return "", common.CodeServerError, fmt.Errorf("recovery account lookup failed")
	}
	if s.redis == nil {
		return "", common.CodeServerError, fmt.Errorf("recovery state store unavailable")
	}
	return normalized, common.CodeSuccess, nil
}

func (s *UserService) ForgotIssueCaptcha(ctx context.Context, email string) ([]byte, common.ErrorCode, error) {
	return s.passwordRecovery().IssueCaptcha(ctx, email)
}
func (s *UserService) ForgotSendOTP(ctx context.Context, email, captcha string) (common.ErrorCode, error) {
	return s.passwordRecovery().SendOTP(ctx, email, captcha)
}
func (s *UserService) ForgotVerifyOTP(ctx context.Context, email, otp string) (string, common.ErrorCode, error) {
	return s.passwordRecovery().VerifyOTP(ctx, email, otp)
}
func (s *UserService) ForgotResetPassword(ctx context.Context, req ForgotResetPasswordRequest, session string) (common.ErrorCode, error) {
	return s.passwordRecovery().ResetPassword(ctx, req, session)
}

func (s *PasswordRecoveryService) IssueCaptcha(ctx context.Context, email string) ([]byte, common.ErrorCode, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	email, code, err := s.ready(ctx, email)
	if err != nil {
		return nil, code, err
	}
	text, err := utility.GenerateCaptchaCode()
	if err != nil {
		return nil, common.CodeServerError, fmt.Errorf("captcha generation failed")
	}
	image, err := utility.RenderCaptchaPNG(text)
	if err != nil {
		return nil, common.CodeServerError, fmt.Errorf("captcha rendering failed")
	}
	if err = s.redis.Set(ctx, utility.CaptchaRedisKey(email), text, time.Minute).Err(); err != nil {
		return nil, common.CodeServerError, fmt.Errorf("captcha state unavailable")
	}
	return image, common.CodeSuccess, nil
}

func recoveryKeys(email string) []string {
	code, attempts, last, lock := utility.OTPRedisKeys(email)
	return []string{utility.CaptchaRedisKey(email), code, attempts, last, lock, utility.OTPVerifiedRedisKey(email), "otp:pending:" + email, "otp:go:reset:" + email}
}

const beginRecoveryOTP = `
if redis.call('EXISTS',KEYS[5])==1 then return {-2} end
if redis.call('EXISTS',KEYS[7])==1 or redis.call('EXISTS',KEYS[8])==1 then return {-3} end
local captcha=redis.call('GET',KEYS[1])
if not captcha then return {0} end
if string.lower(captcha)~=ARGV[1] then return {-4} end
local last=redis.call('GET',KEYS[4])
if last and not tonumber(last) then return redis.error_reply('invalid resend state') end
if last and tonumber(ARGV[4])-tonumber(last)<tonumber(ARGV[6]) then return {-1} end
local snapshot={1}
for _,i in ipairs({2,3,4,6}) do
 local value=redis.call('GET',KEYS[i]); local ttl=redis.call('PTTL',KEYS[i])
 if value and ttl<0 then return redis.error_reply('unbounded recovery state') end
 snapshot[#snapshot+1]=value or '';snapshot[#snapshot+1]=ttl
end
redis.call('DEL',KEYS[1],KEYS[6])
redis.call('SET',KEYS[2],ARGV[2],'PX',ARGV[5])
redis.call('SET',KEYS[3],'0','PX',ARGV[5])
redis.call('SET',KEYS[4],ARGV[4],'PX',ARGV[5])
redis.call('SET',KEYS[7],ARGV[3],'PX',ARGV[5])
return snapshot`

const rollbackRecoveryOTP = `
if redis.call('GET',KEYS[7])~=ARGV[1] or redis.call('GET',KEYS[2])~=ARGV[2] then return 0 end
local offset=4
for _,i in ipairs({2,3,4,6}) do
 local ttl=tonumber(ARGV[offset+1])-tonumber(ARGV[3])
 if ARGV[offset]~='' and ttl>0 then redis.call('SET',KEYS[i],ARGV[offset],'PX',ttl) else redis.call('DEL',KEYS[i]) end
 offset=offset+2
end
redis.call('DEL',KEYS[7]);return 1`

const completeRecoveryOTP = `if redis.call('GET',KEYS[1])==ARGV[1] and redis.call('GET',KEYS[2])==ARGV[2] then redis.call('DEL',KEYS[2]);return 1 else return 0 end`

func (s *PasswordRecoveryService) SendOTP(ctx context.Context, email, captcha string) (common.ErrorCode, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	email, code, err := s.ready(ctx, email)
	if err != nil {
		return code, err
	}
	captcha = strings.ToLower(strings.TrimSpace(captcha))
	if captcha == "" {
		return common.CodeArgumentError, fmt.Errorf("email and captcha required")
	}
	otp, err := utility.GenerateOTPCode()
	if err != nil {
		return common.CodeServerError, fmt.Errorf("otp generation failed")
	}
	salt, err := utility.GenerateOTPSalt()
	if err != nil {
		return common.CodeServerError, fmt.Errorf("otp generation failed")
	}
	nonceBytes, err := utility.GenerateOTPSalt()
	if err != nil {
		return common.CodeServerError, fmt.Errorf("otp generation failed")
	}
	nonce := hex.EncodeToString(nonceBytes)
	stored := utility.EncodeOTPStorageValue(utility.HashOTPCode(otp, salt), salt)
	keys := recoveryKeys(email)
	started := time.Now()
	result, err := s.redis.Eval(ctx, beginRecoveryOTP, keys, captcha, stored, nonce, started.Unix(), utility.OTPTTL.Milliseconds(), int64(utility.OTPResendCooldown/time.Second)).Slice()
	if err != nil {
		return common.CodeServerError, fmt.Errorf("otp state unavailable")
	}
	if len(result) == 0 {
		return common.CodeServerError, fmt.Errorf("invalid otp transition")
	}
	status, ok := result[0].(int64)
	if !ok {
		return common.CodeServerError, fmt.Errorf("invalid otp transition")
	}
	switch status {
	case 0:
		return common.CodeNotEffective, fmt.Errorf("invalid or expired captcha")
	case -4:
		return common.CodeAuthenticationError, fmt.Errorf("invalid or expired captcha")
	case -1:
		return common.CodeNotEffective, fmt.Errorf("resend cooldown is active")
	case -2:
		return common.CodeNotEffective, fmt.Errorf("too many attempts, try later")
	case -3:
		return common.CodeNotEffective, fmt.Errorf("password recovery is in progress")
	}
	if status != 1 || len(result) != 9 {
		return common.CodeServerError, fmt.Errorf("invalid otp transition")
	}
	if err = utility.SendResetCodeEmail(ctx, s.smtp, email, otp, int(utility.OTPTTL/time.Minute)); err != nil {
		cleanup, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancel()
		args := []any{nonce, stored, time.Since(started).Milliseconds()}
		args = append(args, result[1:]...)
		if rollbackErr := s.redis.Eval(cleanup, rollbackRecoveryOTP, keys, args...).Err(); rollbackErr != nil {
			return common.CodeServerError, fmt.Errorf("email failed; recovery state cleanup could not be confirmed")
		}
		return common.CodeServerError, fmt.Errorf("failed to send email")
	}
	cleanup, cleanupCancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cleanupCancel()
	completed, err := s.redis.Eval(cleanup, completeRecoveryOTP, []string{keys[1], keys[6]}, stored, nonce).Int64()
	if err != nil || completed != 1 {
		return common.CodeServerError, fmt.Errorf("email accepted; recovery state could not be confirmed")
	}
	return common.CodeSuccess, nil
}

const verifyRecoveryOTP = `
if redis.call('EXISTS',KEYS[5])==1 then return -2 end
if redis.call('EXISTS',KEYS[7])==1 or redis.call('EXISTS',KEYS[8])==1 then return -3 end
local current=redis.call('GET',KEYS[2]);if not current then return 0 end
if current~=ARGV[1] then return 3 end
if ARGV[2]~='1' then
 local attempts=redis.call('GET',KEYS[3]);if attempts and (not tonumber(attempts) or tonumber(attempts)<0) then return redis.error_reply('invalid attempt state') end
 local count=tonumber(attempts or '0')+1;local ttl=redis.call('PTTL',KEYS[2]);if ttl<=0 then return 0 end
 redis.call('SET',KEYS[3],count,'PX',ttl)
 if count>=tonumber(ARGV[5]) then redis.call('SET',KEYS[5],'1','PX',ARGV[6]) end
 return -1
end
redis.call('SET',KEYS[6],ARGV[3],'PX',ARGV[4])
redis.call('DEL',KEYS[2],KEYS[3],KEYS[4]);return 1`

func (s *PasswordRecoveryService) VerifyOTP(ctx context.Context, email, otp string) (string, common.ErrorCode, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	email, code, err := s.ready(ctx, email)
	if err != nil {
		return "", code, err
	}
	otp = strings.ToUpper(strings.TrimSpace(otp))
	if otp == "" {
		return "", common.CodeArgumentError, fmt.Errorf("email and otp are required")
	}
	keys := recoveryKeys(email)
	stored, err := s.redis.Get(ctx, keys[1]).Result()
	if errors.Is(err, redis.Nil) {
		return "", common.CodeNotEffective, fmt.Errorf("expired otp")
	}
	if err != nil {
		return "", common.CodeServerError, fmt.Errorf("otp state unavailable")
	}
	hash, salt, err := utility.DecodeOTPStorageValue(stored)
	if err != nil {
		return "", common.CodeServerError, fmt.Errorf("otp storage corrupted")
	}
	match := strconv.Itoa(subtle.ConstantTimeCompare([]byte(hash), []byte(utility.HashOTPCode(otp, salt))))
	var nonceBytes [32]byte
	if _, err = rand.Read(nonceBytes[:]); err != nil {
		return "", common.CodeServerError, fmt.Errorf("otp verification unavailable")
	}
	session := hex.EncodeToString(nonceBytes[:])
	result, err := s.redis.Eval(ctx, verifyRecoveryOTP, keys, stored, match, recoveryProof(session), utility.OTPTTL.Milliseconds(), utility.OTPAttemptLimit, utility.OTPAttemptLockDuration.Milliseconds()).Int64()
	if err != nil {
		return "", common.CodeServerError, fmt.Errorf("otp verification unavailable")
	}
	switch result {
	case 1:
		return session, common.CodeSuccess, nil
	case -1:
		return "", common.CodeAuthenticationError, fmt.Errorf("invalid otp")
	case -2:
		return "", common.CodeNotEffective, fmt.Errorf("too many attempts, try later")
	case -3:
		return "", common.CodeNotEffective, fmt.Errorf("password recovery is in progress")
	default:
		return "", common.CodeNotEffective, fmt.Errorf("expired otp")
	}
}

type ForgotResetPasswordRequest struct {
	Email              string `json:"email"`
	NewPassword        string `json:"new_password"`
	ConfirmNewPassword string `json:"confirm_new_password"`
}

const claimRecoveryReset = `
if redis.call('EXISTS',KEYS[2])==1 or redis.call('EXISTS',KEYS[3])==1 then return {0} end
local verified=redis.call('GET',KEYS[1]);local ttl=redis.call('PTTL',KEYS[1]);if not verified or verified~=ARGV[2] or ttl<=0 then return {0} end
redis.call('SET',KEYS[2],ARGV[1],'PX',60000);redis.call('DEL',KEYS[1]);return {1,verified,ttl}`
const restoreRecoveryReset = `
if redis.call('GET',KEYS[2])~=ARGV[1] then return 0 end
local ttl=tonumber(ARGV[3])-tonumber(ARGV[4]);local restored=0
if ttl>0 and redis.call('EXISTS',KEYS[1])==0 and redis.call('EXISTS',KEYS[3])==0 and redis.call('EXISTS',KEYS[4])==0 then redis.call('SET',KEYS[1],ARGV[2],'PX',ttl);restored=1 end
redis.call('DEL',KEYS[2]);return restored`
const clearRecoveryReset = `if redis.call('GET',KEYS[1])==ARGV[1] then redis.call('DEL',KEYS[1]);return 1 else return 0 end`

func (s *PasswordRecoveryService) ResetPassword(ctx context.Context, req ForgotResetPasswordRequest, session string) (common.ErrorCode, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	email, code, err := s.ready(ctx, req.Email)
	if err != nil {
		return code, err
	}
	proof := recoveryProof(session)
	if proof == "" {
		return common.CodeAuthenticationError, fmt.Errorf("recovery session required")
	}
	if req.NewPassword == "" || req.ConfirmNewPassword == "" || len(req.NewPassword) > 4096 {
		return common.CodeArgumentError, fmt.Errorf("email and passwords are required")
	}
	if req.NewPassword != req.ConfirmNewPassword {
		return common.CodeArgumentError, fmt.Errorf("passwords do not match")
	}
	keys := recoveryKeys(email)
	nonceBytes, err := utility.GenerateOTPSalt()
	if err != nil {
		return common.CodeServerError, fmt.Errorf("reset unavailable")
	}
	nonce := hex.EncodeToString(nonceBytes)
	var previous string
	var previousTTL int64
	var userID, hash string
	var claimed bool
	var callbackSucceeded bool
	var started time.Time
	err = s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		user, err := s.user(ctx, tx, email, true)
		if err != nil {
			return err
		}
		userID = user.ID
		claim, err := s.redis.Eval(ctx, claimRecoveryReset, []string{keys[5], keys[7], keys[6]}, nonce, proof).Slice()
		if err != nil {
			return err
		}
		if len(claim) != 3 || claim[0] != int64(1) {
			return errRecoveryNotVerified
		}
		var ok bool
		previous, ok = claim[1].(string)
		if !ok {
			return fmt.Errorf("invalid reset state")
		}
		previousTTL, ok = claim[2].(int64)
		if !ok {
			return fmt.Errorf("invalid reset state")
		}
		claimed = true
		started = time.Now()
		hash, err = s.hashPassword(req.NewPassword)
		if err != nil {
			return err
		}
		result := tx.Model(&entity.User{}).Where("id=?", user.ID).Update("password", hash)
		if result.Error != nil {
			return result.Error
		}
		if result.RowsAffected != 1 {
			return fmt.Errorf("reset account changed")
		}
		callbackSucceeded = true
		return nil
	})
	cleanup, cleanupCancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cleanupCancel()
	if err != nil {
		if errors.Is(err, errRecoveryNotVerified) {
			return common.CodeAuthenticationError, fmt.Errorf("email not verified")
		}
		if claimed {
			// An ordinary MVCC SELECT can see the old password while the original
			// COMMIT is still running. A bounded, independent READ COMMITTED row
			// lock waits for that transaction to settle and keeps recovery fenced.
			committed := false
			readErr := s.db.Session(&gorm.Session{NewDB: true}).WithContext(cleanup).Transaction(func(read *gorm.DB) error {
				var user entity.User
				if e := read.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id=?", userID).Take(&user).Error; e != nil {
					return e
				}
				if hash != "" && user.Password != nil && *user.Password == hash {
					committed = true
					return nil
				}
				// A successful callback followed by a COMMIT error is uncertain.
				// A different current hash cannot prove that COMMIT never happened;
				// retain the claim and require a new verification, never restore it.
				if callbackSucceeded {
					return errRecoveryCommitUncertain
				}
				return s.redis.Eval(cleanup, restoreRecoveryReset, []string{keys[5], keys[7], keys[6], keys[1]}, nonce, previous, previousTTL, time.Since(started).Milliseconds()).Err()
			}, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
			if readErr == nil && committed {
				err = nil
			} else if callbackSucceeded {
				return common.CodeServerError, fmt.Errorf("password reset outcome could not be confirmed; verify again later")
			} else if readErr != nil {
				return common.CodeServerError, fmt.Errorf("reset failed; verification cleanup could not be confirmed")
			}
		}
		if err != nil {
			return common.CodeServerError, fmt.Errorf("failed to reset password")
		}
	}
	cleared, err := s.redis.Eval(cleanup, clearRecoveryReset, []string{keys[7]}, nonce).Int64()
	if err != nil || cleared != 1 {
		return common.CodeServerError, fmt.Errorf("password updated; recovery cleanup could not be confirmed")
	}
	return common.CodeSuccess, nil
}

var errRecoveryNotVerified = errors.New("email not verified")
var errRecoveryCommitUncertain = errors.New("password reset commit outcome is uncertain")

// recoveryProof never stores the bearer credential in Redis. Its Go-only key
// namespace also prevents the historical Python email flag from authorizing Go.
func recoveryProof(token string) string {
	decoded, err := hex.DecodeString(token)
	if err != nil || len(decoded) != 32 {
		return ""
	}
	digest := sha256.Sum256(decoded)
	return "session:" + hex.EncodeToString(digest[:])
}
