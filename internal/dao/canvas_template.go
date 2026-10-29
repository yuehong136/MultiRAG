package dao

import (
	"context"

	"multirag/internal/entity"
)

// CanvasTemplateDAO reads the shared multilingual system template catalogue.
type CanvasTemplateDAO struct{}

func NewCanvasTemplateDAO() *CanvasTemplateDAO { return &CanvasTemplateDAO{} }

// GetAll keeps complete locale maps and DSL; equal timestamps have stable order.
func (dao *CanvasTemplateDAO) GetAll(ctx context.Context) ([]*entity.CanvasTemplate, error) {
	rows := []*entity.CanvasTemplate{}
	if err := DB.WithContext(ctx).Order("create_time DESC").Order("id ASC").Find(&rows).Error; err != nil {
		return nil, err
	}
	return rows, nil
}
