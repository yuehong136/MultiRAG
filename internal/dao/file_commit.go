package dao

import (
	"context"
	"errors"
	"gorm.io/gorm"
	"multirag/internal/entity"
)

// FileCommitDAO always carries the request transaction and resolved tenant/scope.
type FileCommitDAO struct {
	DB                 *gorm.DB
	TenantID, FolderID string
}

func (d FileCommitDAO) query(ctx context.Context) *gorm.DB {
	return d.DB.WithContext(ctx).Model(&entity.FileCommit{}).Where("tenant_id = ? AND folder_id = ?", d.TenantID, d.FolderID)
}
func (d FileCommitDAO) Get(ctx context.Context, id string) (*entity.FileCommit, error) {
	var c entity.FileCommit
	err := d.query(ctx).Where("id = ?", id).First(&c).Error
	return &c, err
}
func (d FileCommitDAO) Latest(ctx context.Context) (*entity.FileCommit, error) {
	var c entity.FileCommit
	err := d.query(ctx).Order("sequence DESC").First(&c).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, nil
	}
	return &c, err
}
func (d FileCommitDAO) List(ctx context.Context, page, size int, desc bool) ([]entity.FileCommit, int64, error) {
	var count int64
	result := []entity.FileCommit{}
	if err := d.query(ctx).Count(&count).Error; err != nil {
		return nil, 0, err
	}
	order := "sequence ASC"
	if desc {
		order = "sequence DESC"
	}
	err := d.query(ctx).Order(order).Offset((page - 1) * size).Limit(size).Find(&result).Error
	return result, count, err
}
func (d FileCommitDAO) Items(ctx context.Context, id string) ([]entity.FileCommitItem, error) {
	if _, err := d.Get(ctx, id); err != nil {
		return nil, err
	}
	result := []entity.FileCommitItem{}
	err := d.DB.WithContext(ctx).Where("commit_id = ?", id).Order("file_id").Find(&result).Error
	return result, err
}
