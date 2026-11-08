package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"unicode/utf8"

	"github.com/google/uuid"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
)

// SaveAgentRequest has no caller-controlled owner or primary key.
type SaveAgentRequest struct {
	Title       *string         `json:"title,omitempty"`
	Description *string         `json:"description,omitempty"`
	Permission  *string         `json:"permission,omitempty"`
	CanvasType  *string         `json:"canvas_type,omitempty"`
	DSL         json.RawMessage `json:"dsl,omitempty"`
}

func decodeAgentDSL(raw json.RawMessage) (entity.JSONMap, error) {
	var value entity.JSONMap
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	if err := d.Decode(&value); err != nil || value == nil {
		return nil, fmt.Errorf("dsl must be a JSON object")
	}
	if err := d.Decode(new(any)); !errors.Is(err, io.EOF) {
		return nil, fmt.Errorf("dsl must contain one JSON object")
	}
	return value, nil
}

func validateAgentSave(req *SaveAgentRequest, create bool) (entity.JSONMap, error) {
	if req == nil {
		return nil, fmt.Errorf("request is required")
	}
	if create && (req.Title == nil || req.DSL == nil) {
		return nil, fmt.Errorf("title and dsl are required")
	}
	if req.Title != nil && (strings.TrimSpace(*req.Title) == "" || utf8.RuneCountInString(*req.Title) > 255) {
		return nil, fmt.Errorf("title must contain 1 to 255 characters")
	}
	if req.CanvasType != nil && utf8.RuneCountInString(*req.CanvasType) > 32 {
		return nil, fmt.Errorf("canvas_type is too long")
	}
	if req.Permission != nil && *req.Permission != "me" && *req.Permission != "team" {
		return nil, fmt.Errorf("permission must be me or team")
	}
	if req.DSL != nil {
		return decodeAgentDSL(req.DSL)
	}
	return nil, nil
}

func (s *AgentService) GetAgent(ctx context.Context, actorID, canvasID string) (*dao.AgentCanvas, common.ErrorCode, error) {
	if _, code, err := s.requireCanvasAccess(ctx, actorID, canvasID); err != nil {
		return nil, code, err
	}
	row, err := dao.NewUserCanvasVersionDAO().LoadAccessibleCanvas(ctx, actorID, canvasID)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, common.CodeOperatingError, fmt.Errorf("Agent not found or no permission.")
	}
	if err != nil {
		return nil, common.CodeServerError, fmt.Errorf("Unable to read agent.")
	}
	return row, common.CodeSuccess, nil
}

func (s *AgentService) CreateAgent(ctx context.Context, actorID string, req *SaveAgentRequest) (*entity.UserCanvas, common.ErrorCode, error) {
	if actorID == "" {
		return nil, common.CodeAuthenticationError, fmt.Errorf("No authorization.")
	}
	dsl, err := validateAgentSave(req, true)
	if err != nil {
		return nil, common.CodeArgumentError, err
	}
	row := &entity.UserCanvas{ID: strings.ReplaceAll(uuid.NewString(), "-", ""), UserID: actorID, Title: req.Title, Description: req.Description, CanvasType: req.CanvasType, Permission: "me", CanvasCategory: "agent_canvas", DSL: dsl}
	if req.Permission != nil {
		row.Permission = *req.Permission
	}
	trimmed := strings.TrimSpace(*row.Title)
	row.Title = &trimmed
	err = dao.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var owner entity.User
		if err := tx.Model(&entity.User{}).Select("t_ai_users.id").Clauses(clause.Locking{Strength: "UPDATE", Table: clause.Table{Name: "t_ai_users"}}).Joins("JOIN t_ai_tenants ON t_ai_tenants.id = t_ai_users.id AND t_ai_tenants.status = ?", "1").Where("t_ai_users.id = ? AND t_ai_users.status = ? AND t_ai_users.is_active = ?", actorID, "1", true).First(&owner).Error; err != nil {
			return err
		}
		var duplicates int64
		if err := tx.Model(&entity.UserCanvas{}).Where("user_id = ? AND title = ? AND canvas_category = ?", actorID, *row.Title, row.CanvasCategory).Count(&duplicates).Error; err != nil {
			return err
		}
		if duplicates > 0 {
			return errAgentTitleExists
		}
		return tx.Create(row).Error
	})
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, common.CodeOperatingError, fmt.Errorf("Active agent owner is required.")
	}
	if errors.Is(err, errAgentTitleExists) {
		return nil, common.CodeDataError, fmt.Errorf("An agent with this title already exists.")
	}
	if err != nil {
		return nil, common.CodeServerError, fmt.Errorf("Unable to create agent.")
	}
	return row, common.CodeSuccess, nil
}

var errAgentTitleExists = errors.New("agent title already exists")

// Mutations are owner-only, even when TEAM permission grants read/run access.
func (s *AgentService) mutateAgent(ctx context.Context, actorID, canvasID string, f func(*gorm.DB, *dao.AgentCanvas) error) (common.ErrorCode, error) {
	row, code, err := s.requireCanvasAccess(ctx, actorID, canvasID)
	if err != nil {
		return code, err
	}
	if row.UserID != actorID {
		return common.CodeOperatingError, fmt.Errorf("Only owner of canvas authorized for this operation.")
	}
	err = dao.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var owner entity.User
		if err := tx.Select("id").Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ? AND status = ? AND is_active = ?", actorID, "1", true).First(&owner).Error; err != nil {
			return err
		}
		var locked dao.AgentCanvas
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Model(&entity.UserCanvas{}).Where("id = ? AND user_id = ?", canvasID, actorID).First(&locked).Error; err != nil {
			return err
		}
		return f(tx, &locked)
	})
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return common.CodeOperatingError, fmt.Errorf("Agent not found or no permission.")
	}
	if errors.Is(err, errAgentTitleExists) {
		return common.CodeDataError, fmt.Errorf("An agent with this title already exists.")
	}
	if err != nil {
		return common.CodeServerError, fmt.Errorf("Unable to save agent.")
	}
	return common.CodeSuccess, nil
}

func (s *AgentService) UpdateAgent(ctx context.Context, actorID, canvasID string, req *SaveAgentRequest) (common.ErrorCode, error) {
	dsl, err := validateAgentSave(req, false)
	if err != nil {
		return common.CodeArgumentError, err
	}
	return s.mutateAgent(ctx, actorID, canvasID, func(tx *gorm.DB, row *dao.AgentCanvas) error {
		updates := map[string]any{}
		if req.Title != nil {
			var duplicates int64
			if err := tx.Model(&entity.UserCanvas{}).Where("id <> ? AND user_id = ? AND title = ? AND canvas_category = ?", row.ID, actorID, strings.TrimSpace(*req.Title), row.CanvasCategory).Count(&duplicates).Error; err != nil {
				return err
			}
			if duplicates > 0 {
				return errAgentTitleExists
			}
			updates["title"] = strings.TrimSpace(*req.Title)
		}
		if req.Description != nil {
			updates["description"] = *req.Description
		}
		if req.CanvasType != nil {
			updates["canvas_type"] = *req.CanvasType
		}
		if req.Permission != nil {
			updates["permission"] = *req.Permission
		}
		if dsl != nil {
			updates["dsl"] = dsl
			updates["release"] = false
		}
		if len(updates) == 0 {
			return nil
		}
		return tx.Model(&entity.UserCanvas{}).Where("id = ? AND user_id = ?", row.ID, actorID).Updates(updates).Error
	})
}

func (s *AgentService) DeleteAgent(ctx context.Context, actorID, canvasID string) (common.ErrorCode, error) {
	return s.mutateAgent(ctx, actorID, canvasID, func(tx *gorm.DB, row *dao.AgentCanvas) error {
		if err := tx.Where("user_canvas_id = ?", row.ID).Delete(&entity.UserCanvasVersion{}).Error; err != nil {
			return err
		}
		return tx.Where("id = ? AND user_id = ?", row.ID, actorID).Delete(&entity.UserCanvas{}).Error
	})
}

func (s *AgentService) PublishAgent(ctx context.Context, actorID, canvasID string) (*entity.UserCanvasVersion, common.ErrorCode, error) {
	var version *entity.UserCanvasVersion
	code, err := s.mutateAgent(ctx, actorID, canvasID, func(tx *gorm.DB, row *dao.AgentCanvas) error {
		dsl, err := decodeAgentDSL(row.DSL)
		if err != nil {
			return err
		}
		version = &entity.UserCanvasVersion{ID: strings.ReplaceAll(uuid.NewString(), "-", ""), UserCanvasID: row.ID, Title: row.Title, Description: row.Description, DSL: dsl, Release: true}
		if err := tx.Create(version).Error; err != nil {
			return err
		}
		return tx.Model(&entity.UserCanvas{}).Where("id = ? AND user_id = ?", row.ID, actorID).Update("release", true).Error
	})
	return version, code, err
}

func (s *AgentService) DeleteVersion(ctx context.Context, actorID, canvasID, versionID string) (common.ErrorCode, error) {
	if versionID == "" {
		return common.CodeArgumentError, fmt.Errorf("version_id is required")
	}
	return s.mutateAgent(ctx, actorID, canvasID, func(tx *gorm.DB, row *dao.AgentCanvas) error {
		result := tx.Where("id = ? AND user_canvas_id = ?", versionID, row.ID).Delete(&entity.UserCanvasVersion{})
		if result.Error != nil {
			return result.Error
		}
		if result.RowsAffected != 1 {
			return gorm.ErrRecordNotFound
		}
		var remaining int64
		if err := tx.Model(&entity.UserCanvasVersion{}).Where("user_canvas_id = ? AND release = ? AND dsl = ?", row.ID, true, string(row.DSL)).Count(&remaining).Error; err != nil {
			return err
		}
		return tx.Model(&entity.UserCanvas{}).Where("id = ? AND user_id = ?", row.ID, actorID).Update("release", row.Release && remaining > 0).Error
	})
}
