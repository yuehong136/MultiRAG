//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

package dao

import (
	"context"
	"errors"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/entity"
	"strings"
)

type MCPServerDAO struct{}

func NewMCPServerDAO() *MCPServerDAO { return &MCPServerDAO{} }

// CreateMCPServer serializes duplicate-name checks on the existing tenant row.
func (dao *MCPServerDAO) CreateMCPServer(ctx context.Context, server *entity.MCPServer) error {
	return DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var tenant entity.Tenant
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ?", server.TenantID).Take(&tenant).Error; err != nil {
			return errors.New("Tenant unavailable.")
		}
		var count int64
		if err := tx.Model(&entity.MCPServer{}).Where("tenant_id = ? AND name = ?", server.TenantID, server.Name).Count(&count).Error; err != nil {
			return errors.New("Unable to read MCP servers.")
		}
		if count != 0 {
			return errors.New("Duplicated MCP server name.")
		}
		if err := tx.Create(server).Error; err != nil {
			return errors.New("Failed to create MCP server.")
		}
		return nil
	})
}

// MCPListQuery keeps an absent ID filter (nil) distinct from an empty one.
type MCPListQuery struct {
	IDs            []string
	Keywords       string
	Page, PageSize int
	OrderBy        string
	Desc           bool
}

// MCPServerOrderColumn maps public names to a closed SQL column allowlist.
func MCPServerOrderColumn(field string) (string, error) {
	switch field {
	case "id", "name", "server_type", "url", "create_time", "update_time", "create_date", "update_date":
		return field, nil
	default:
		return "", errors.New("Invalid MCP ordering field.")
	}
}

// ListMCPServers applies pagination in SQL and never selects connection headers.
func (dao *MCPServerDAO) ListMCPServers(ctx context.Context, tx *gorm.DB, tenantID string, opts MCPListQuery) ([]entity.MCPServer, int64, error) {
	rows := []entity.MCPServer{}
	column, err := MCPServerOrderColumn(opts.OrderBy)
	if err != nil {
		return nil, 0, err
	}
	query := tx.WithContext(ctx).Model(&entity.MCPServer{}).Where("tenant_id = ?", tenantID)
	if opts.IDs != nil {
		query = query.Where("id IN ?", opts.IDs)
	}
	if opts.Keywords != "" {
		literal := strings.NewReplacer("\\", "\\\\", "%", "\\%", "_", "\\_").Replace(strings.ToLower(opts.Keywords))
		query = query.Where("LOWER(name) LIKE ?", "%"+literal+"%")
	}
	var total int64
	if err := query.Count(&total).Error; err != nil {
		return nil, 0, err
	}
	query = query.Select("id", "name", "server_type", "url", "description", "variables", "create_date", "update_date").Order(clause.OrderByColumn{Column: clause.Column{Name: column}, Desc: opts.Desc})
	if column != "id" {
		query = query.Order(clause.OrderByColumn{Column: clause.Column{Name: "id"}, Desc: opts.Desc})
	}
	if opts.Page > 0 && opts.PageSize > 0 {
		query = query.Offset((opts.Page - 1) * opts.PageSize).Limit(opts.PageSize)
	}
	err = query.Find(&rows).Error
	return rows, total, err
}
