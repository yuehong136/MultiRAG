package service

import (
	"context"
	"database/sql"
	"errors"
	"gorm.io/gorm"
	"multirag/internal/dao"
	"multirag/internal/utility"
	"sync"
	"testing"
)

var errRecoveryCommitFault = errors.New("controlled commit acknowledgement loss")

// Only the first transaction is faulted. SQL, row locks, COMMIT and ROLLBACK
// still use the real scratch PostgreSQL connection; subsequent readback uses
// another real transaction from the pool.
type recoveryCommitFaultPool struct {
	*sql.DB
	once     sync.Once
	commit   func(*sql.Tx) error
	keepOpen bool
}

func (p *recoveryCommitFaultPool) BeginTx(ctx context.Context, options *sql.TxOptions) (gorm.ConnPool, error) {
	tx, err := p.DB.BeginTx(ctx, options)
	if err != nil {
		return nil, err
	}
	fault := false
	p.once.Do(func() { fault = true })
	if fault {
		return &recoveryCommitFaultTx{Tx: tx, commit: p.commit, keepOpen: p.keepOpen}, nil
	}
	return tx, nil
}

type recoveryCommitFaultTx struct {
	*sql.Tx
	commit   func(*sql.Tx) error
	keepOpen bool
}

func (t *recoveryCommitFaultTx) Commit() error { return t.commit(t.Tx) }
func (t *recoveryCommitFaultTx) Rollback() error {
	if t.keepOpen {
		return errRecoveryCommitFault
	}
	return t.Tx.Rollback()
}

func TestForgotResetUnknownCommitDoesNotRestoreProof(t *testing.T) {
	for _, commitActually := range []bool{false, true} {
		t.Run(map[bool]string{false: "rolled-back", true: "committed"}[commitActually], func(t *testing.T) {
			f, s, _, email, encrypt := canonicalRecoveryFixture(t)
			proof := utility.OTPVerifiedRedisKey(email)
			if err := f.Redis.Set(t.Context(), proof, "1", 0).Err(); err != nil {
				t.Fatal(err)
			}
			pool, err := f.DB.DB()
			if err != nil {
				t.Fatal(err)
			}
			controlled := &recoveryCommitFaultPool{DB: pool, commit: func(tx *sql.Tx) error {
				if commitActually {
					if err := tx.Commit(); err != nil {
						return err
					}
				} else {
					if err := tx.Rollback(); err != nil {
						return err
					}
				}
				return errRecoveryCommitFault
			}}
			db := f.DB.Session(&gorm.Session{NewDB: true})
			db.Config.ConnPool = controlled
			db.Statement.ConnPool = controlled
			dao.DB = db
			request := &ForgotResetPasswordRequest{Email: email, NewPassword: encrypt("changed-password"), ConfirmNewPassword: encrypt("changed-password")}
			if user, _, err := s.ForgotResetPassword(request); err == nil || user != nil {
				t.Fatal("unknown commit acknowledged success")
			}
			if f.Redis.Exists(t.Context(), proof).Val() != 0 {
				t.Fatal("unknown commit restored proof")
			}
			if user, _, err := s.ForgotResetPassword(request); err == nil || user != nil {
				t.Fatal("unknown commit reused proof")
			}
			var password string
			if err := f.Read.QueryRow("SELECT password FROM user WHERE email=?", email).Scan(&password); err != nil {
				t.Fatal(err)
			}
			if s.VerifyPassword(password, "changed-password") != commitActually {
				t.Fatal("independent SQL disagrees with actual transaction outcome")
			}
			f.Journal.Event("recovery-unknown-commit-readback", map[string]any{"committed": commitActually, "proof_absent": true, "replay_rejected": true})
		})
	}
}
