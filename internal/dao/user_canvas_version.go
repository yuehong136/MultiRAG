package dao

import (
	"context"
	"multirag/internal/entity"
)

// UserCanvasVersionDAO reads version data without loading DSL for list requests.
type UserCanvasVersionDAO struct{}

func NewUserCanvasVersionDAO() *UserCanvasVersionDAO { return &UserCanvasVersionDAO{} }

// AgentVersionSummary retains the published flag and description used by current consumers.
type AgentVersionSummary struct {
	ID           string  `json:"id"`
	UserCanvasID string  `json:"user_canvas_id"`
	Title        *string `json:"title"`
	Description  *string `json:"description"`
	Release      bool    `json:"release"`
	entity.BaseModel
}

func (d *UserCanvasVersionDAO) ListByCanvasID(ctx context.Context, canvasID string) ([]*AgentVersionSummary, error) {
	rows := []*AgentVersionSummary{}
	err := DB.WithContext(ctx).Model(&entity.UserCanvasVersion{}).
		Select("id,user_canvas_id,title,description,release,create_time,update_time,create_date,update_date").
		Where("user_canvas_id = ?", canvasID).Order("update_time DESC").Order("id DESC").Find(&rows).Error
	return rows, err
}

func (d *UserCanvasVersionDAO) GetByCanvasID(ctx context.Context, canvasID, versionID string) (*entity.UserCanvasVersion, error) {
	var row entity.UserCanvasVersion
	if err := DB.WithContext(ctx).Where("id = ? AND user_canvas_id = ?", versionID, canvasID).First(&row).Error; err != nil {
		return nil, err
	}
	return &row, nil
}

// AccessibleCanvas excludes disabled owners/tenants and invited or revoked memberships.
func (d *UserCanvasVersionDAO) AccessibleCanvas(ctx context.Context, userID, canvasID string) (*entity.UserCanvas, error) {
	var row entity.UserCanvas
	err := DB.WithContext(ctx).Model(&entity.UserCanvas{}).
		Select("t_ai_user_canvases.id,t_ai_user_canvases.user_id,t_ai_user_canvases.permission").
		Joins("JOIN t_ai_users AS owner ON owner.id = t_ai_user_canvases.user_id AND owner.status = ? AND owner.is_active = ?", "1", true).
		Joins("JOIN t_ai_tenants AS tenant ON tenant.id = t_ai_user_canvases.user_id AND tenant.status = ?", "1").
		Where("t_ai_user_canvases.id = ?", canvasID).
		Where("EXISTS (SELECT 1 FROM t_ai_users AS actor WHERE actor.id = ? AND actor.status = ? AND actor.is_active = ?)", userID, "1", true).
		Where("t_ai_user_canvases.user_id = ? OR (t_ai_user_canvases.permission = ? AND EXISTS (SELECT 1 FROM t_ai_user_tenants AS membership WHERE membership.user_id = ? AND membership.tenant_id = t_ai_user_canvases.user_id AND membership.status = ? AND membership.role IN ?))", userID, "team", userID, "1", []string{"normal", "admin"}).
		First(&row).Error
	return &row, err
}

// CountTenantDocuments follows the knowledgebase owner rather than the uploader.
func (d *UserCanvasVersionDAO) CountTenantDocuments(ctx context.Context, tenantID string) (int64, error) {
	var count int64
	err := DB.WithContext(ctx).Model(&entity.Document{}).
		Joins("JOIN t_ai_knowledgebases AS kb ON kb.id = t_ai_documents.kb_id").Where("kb.tenant_id = ?", tenantID).Count(&count).Error
	return count, err
}
