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
