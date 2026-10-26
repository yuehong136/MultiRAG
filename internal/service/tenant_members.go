package service

import (
	"context"
	"errors"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"net/mail"
	"strings"
)

// TenantMember exposes only member profile and invitation state.
type TenantMember struct {
	ID       string  `json:"id"`
	UserID   string  `json:"user_id"`
	Role     string  `json:"role"`
	Status   string  `json:"status"`
	Nickname string  `json:"nickname"`
	Email    string  `json:"email"`
	Avatar   *string `json:"avatar"`
}

type AddMemberRequest struct {
	Email string `json:"email"`
}

var errTenantMemberDenied = errors.New("No authorization.")
var errTenantMemberInvalid = errors.New("Invalid member operation.")

// lockMemberTenant serializes invitations, acceptance and removal on the tenant.
func lockMemberTenant(tx *gorm.DB, tenantID string) error {
	var tenant entity.Tenant
	return tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ? AND status = ?", tenantID, "1").Take(&tenant).Error
}
func verifyTenantOwner(tx *gorm.DB, userID, tenantID string) error {
	if userID == "" || userID != tenantID {
		return errTenantMemberDenied
	}
	var count int64
	if err := tx.Model(&entity.UserTenant{}).Where("user_id = ? AND tenant_id = ? AND status = ? AND role = ?", userID, tenantID, "1", "owner").Count(&count).Error; err != nil {
		return err
	}
	if count != 1 {
		return errTenantMemberDenied
	}
	return nil
}
func tenantMemberCode(err error) common.ErrorCode {
	if errors.Is(err, errTenantMemberDenied) {
		return common.CodeAuthenticationError
	}
	if errors.Is(err, errTenantMemberInvalid) || errors.Is(err, gorm.ErrRecordNotFound) {
		return common.CodeArgumentError
	}
	return common.CodeServerError
}
func (s *TenantService) ListMembers(ctx context.Context, userID, tenantID string) ([]TenantMember, common.ErrorCode, error) {
	rows := []TenantMember{}
	err := dao.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if err := verifyTenantOwner(tx, userID, tenantID); err != nil {
			return err
		}
		return tx.Table("t_ai_user_tenants AS m").Select("m.id, m.user_id, m.role, m.status, u.nickname, u.email, u.avatar").Joins("JOIN t_ai_users AS u ON u.id = m.user_id").Where("m.tenant_id = ? AND m.status = ? AND m.role != ?", tenantID, "1", "owner").Order("m.create_time ASC, m.id ASC").Scan(&rows).Error
	})
	if err != nil {
		return nil, tenantMemberCode(err), errors.New("Unable to list tenant members.")
	}
	return rows, common.CodeSuccess, nil
}
func (s *TenantService) AddMember(ctx context.Context, userID, tenantID string, req *AddMemberRequest) (*TenantMember, common.ErrorCode, error) {
	if userID == "" || userID != tenantID {
		return nil, common.CodeAuthenticationError, errTenantMemberDenied
	}
	if req == nil {
		return nil, common.CodeArgumentError, errTenantMemberInvalid
	}
	address, err := mail.ParseAddress(req.Email)
	if err != nil || address.Address != req.Email || strings.TrimSpace(req.Email) != req.Email {
		return nil, common.CodeArgumentError, errTenantMemberInvalid
	}
	var result TenantMember
	err = dao.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if err := lockMemberTenant(tx, tenantID); err != nil {
			return err
		}
		if err := verifyTenantOwner(tx, userID, tenantID); err != nil {
			return err
		}
		var invitee entity.User
		if err := tx.Where("email = ? AND status = ? AND is_active = ?", req.Email, "1", true).Take(&invitee).Error; err != nil {
			return err
		}
		var count int64
		if err := tx.Model(&entity.UserTenant{}).Where("tenant_id = ? AND user_id = ? AND status = ?", tenantID, invitee.ID, "1").Count(&count).Error; err != nil {
			return err
		}
		if count != 0 || invitee.ID == tenantID {
			return errTenantMemberInvalid
		}
		status := "1"
		row := entity.UserTenant{ID: common.GenerateUUID(), UserID: invitee.ID, TenantID: tenantID, Role: "invite", InvitedBy: userID, Status: &status}
		if err := tx.Create(&row).Error; err != nil {
			return err
		}
		result = TenantMember{ID: row.ID, UserID: invitee.ID, Role: row.Role, Status: status, Nickname: invitee.Nickname, Email: invitee.Email, Avatar: invitee.Avatar}
		return nil
	})
	if err != nil {
		return nil, tenantMemberCode(err), errors.New("Unable to invite tenant member.")
	}
	return &result, common.CodeSuccess, nil
}
func (s *TenantService) RemoveMember(ctx context.Context, userID, tenantID, targetID string) (common.ErrorCode, error) {
	if userID == "" || (userID != tenantID && userID != targetID) {
		return common.CodeAuthenticationError, errTenantMemberDenied
	}
	if targetID == "" || targetID == tenantID {
		return common.CodeArgumentError, errTenantMemberInvalid
	}
	err := dao.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if err := lockMemberTenant(tx, tenantID); err != nil {
			return err
		}
		if userID == tenantID {
			if err := verifyTenantOwner(tx, userID, tenantID); err != nil {
				return err
			}
		}
		result := tx.Where("tenant_id = ? AND user_id = ? AND role != ? AND status = ?", tenantID, targetID, "owner", "1").Delete(&entity.UserTenant{})
		if result.Error != nil {
			return result.Error
		}
		if result.RowsAffected != 1 {
			return errTenantMemberInvalid
		}
		return nil
	})
	if err != nil {
		return tenantMemberCode(err), errors.New("Unable to remove tenant member.")
	}
	return common.CodeSuccess, nil
}
func (s *TenantService) AcceptInvite(ctx context.Context, userID, tenantID string) (common.ErrorCode, error) {
	if userID == "" {
		return common.CodeAuthenticationError, errTenantMemberDenied
	}
	err := dao.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if err := lockMemberTenant(tx, tenantID); err != nil {
			return err
		}
		result := tx.Model(&entity.UserTenant{}).Where("tenant_id = ? AND user_id = ? AND role = ? AND status = ?", tenantID, userID, "invite", "1").Update("role", "normal")
		if result.Error != nil {
			return result.Error
		}
		if result.RowsAffected != 1 {
			return errTenantMemberInvalid
		}
		return nil
	})
	if err != nil {
		return tenantMemberCode(err), errors.New("Unable to accept tenant invitation.")
	}
	return common.CodeSuccess, nil
}
