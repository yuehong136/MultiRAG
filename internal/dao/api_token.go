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
	"encoding/json"
	"fmt"
	"multirag/internal/entity"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

// APITokenDAO API token data access object
type APITokenDAO struct{}

// NewAPITokenDAO create API token DAO
func NewAPITokenDAO() *APITokenDAO {
	return &APITokenDAO{}
}

// Create creates a new API token
func (dao *APITokenDAO) Create(apiToken *entity.APIToken) error {
	return DB.Create(apiToken).Error
}

// GetByTenantID gets API tokens by tenant ID
func (dao *APITokenDAO) GetByTenantID(tenantID string) ([]*entity.APIToken, error) {
	var tokens []*entity.APIToken
	err := DB.Where("tenant_id = ?", tenantID).Find(&tokens).Error
	return tokens, err
}

// DeleteByTenantID deletes all API tokens by tenant ID (hard delete)
func (dao *APITokenDAO) DeleteByTenantID(tenantID string) (int64, error) {
	result := DB.Unscoped().Where("tenant_id = ?", tenantID).Delete(&entity.APIToken{})
	return result.RowsAffected, result.Error
}

// GetUserByAPIToken gets API token by access key
func (dao *APITokenDAO) GetUserByAPIToken(token string) (*entity.APIToken, error) {
	return dao.GetUserByAPITokenContext(context.Background(), token)
}

// GetUserByAPITokenContext preserves the single token-to-tenant binding and
// refuses disabled tenants. A duplicate key must never choose an arbitrary owner.
func (dao *APITokenDAO) GetUserByAPITokenContext(ctx context.Context, token string) (*entity.APIToken, error) {
	var tokens []entity.APIToken
	if err := DB.WithContext(ctx).Where("token = ?", token).Limit(2).Find(&tokens).Error; err != nil {
		return nil, err
	}
	if len(tokens) != 1 || tokens[0].TenantID == "" {
		return nil, gorm.ErrRecordNotFound
	}
	var active int64
	if err := DB.WithContext(ctx).Model(&entity.Tenant{}).Where("id = ? AND status = ?", tokens[0].TenantID, "1").Count(&active).Error; err != nil {
		return nil, err
	}
	if active != 1 {
		return nil, gorm.ErrRecordNotFound
	}
	return &tokens[0], nil
}

// DeleteByDialogIDs deletes API tokens by dialog IDs (hard delete)
func (dao *APITokenDAO) DeleteByDialogIDs(dialogIDs []string) (int64, error) {
	if len(dialogIDs) == 0 {
		return 0, nil
	}
	result := DB.Unscoped().Where("dialog_id IN ?", dialogIDs).Delete(&entity.APIToken{})
	return result.RowsAffected, result.Error
}

// DeleteByTenantIDAndToken deletes a specific API token by tenant ID and token value
func (dao *APITokenDAO) DeleteByTenantIDAndToken(tenantID, token string) (int64, error) {
	result := DB.Unscoped().Where("tenant_id = ? AND token = ?", tenantID, token).Delete(&entity.APIToken{})
	return result.RowsAffected, result.Error
}

// API4ConversationDAO API for conversation data access object
type API4ConversationDAO struct{}

// NewAPI4ConversationDAO create API4Conversation DAO
func NewAPI4ConversationDAO() *API4ConversationDAO {
	return &API4ConversationDAO{}
}

// DeleteByDialogIDs deletes API4Conversations by dialog IDs (hard delete)
func (dao *API4ConversationDAO) DeleteByDialogIDs(dialogIDs []string) (int64, error) {
	if len(dialogIDs) == 0 {
		return 0, nil
	}
	result := DB.Unscoped().Where("dialog_id IN ?", dialogIDs).Delete(&entity.API4Conversation{})
	return result.RowsAffected, result.Error
}

// BackfillBeta performs a compare-and-set and returns the persisted concurrent winner.
func (dao *APITokenDAO) BackfillBeta(tenantID, token, candidate string) (string, error) {
	if candidate == "" {
		return "", fmt.Errorf("empty beta candidate")
	}
	result := DB.Model(&entity.APIToken{}).Where("tenant_id = ? AND token = ? AND (beta IS NULL OR beta = '')", tenantID, token).Update("beta", candidate)
	if result.Error != nil {
		return "", result.Error
	}
	var stored entity.APIToken
	if err := DB.Where("tenant_id = ? AND token = ?", tenantID, token).First(&stored).Error; err != nil {
		return "", err
	}
	if stored.Beta == nil || *stored.Beta == "" {
		return "", fmt.Errorf("beta readback is empty")
	}
	return *stored.Beta, nil
}

// AgentSession keeps immutable DSL JSON numeric identifiers lossless on read.
// It shadows the legacy JSONMap scanner only for the agent session endpoints.
type AgentSession struct {
	entity.API4Conversation
	DSL json.RawMessage `gorm:"column:dsl" json:"dsl,omitempty"`
}

func (d *API4ConversationDAO) GetBySessionID(ctx context.Context, sessionID, agentID string) (*AgentSession, error) {
	var row AgentSession
	err := DB.WithContext(ctx).Model(&entity.API4Conversation{}).
		Where("id = ? AND dialog_id = ? AND source = ?", sessionID, agentID, "agent").First(&row).Error
	if err != nil {
		return nil, err
	}
	return &row, nil
}

// DeleteBySessionIDAndAgentID cannot delete another agent's or ordinary chat session.
func (d *API4ConversationDAO) DeleteBySessionIDAndAgentID(ctx context.Context, sessionID, agentID string) (int64, error) {
	result := DB.WithContext(ctx).Where("id = ? AND dialog_id = ? AND source = ?", sessionID, agentID, "agent").Delete(&entity.API4Conversation{})
	return result.RowsAffected, result.Error
}

// ListIDsByAgentID never includes ordinary chat sessions sharing the dialog ID.
func (d *API4ConversationDAO) ListIDsByAgentID(ctx context.Context, agentID string) ([]string, error) {
	ids := []string{}
	err := DB.WithContext(ctx).Model(&entity.API4Conversation{}).Where("dialog_id = ? AND source = ?", agentID, "agent").Order("id").Pluck("id", &ids).Error
	return ids, err
}

// DeleteAgentSessionsOwned holds the owner row and commits all SQL deletes together.
// Business-level missing IDs are returned separately from storage failures.
func (d *API4ConversationDAO) DeleteAgentSessionsOwned(ctx context.Context, actorID, agentID string, ids []string, deleteAll bool) (int, []string, error) {
	count := 0
	missing := []string{}
	err := DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var parent entity.UserCanvas
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Select("id").
			Where("id = ? AND user_id = ?", agentID, actorID).
			Where("EXISTS (SELECT 1 FROM t_ai_users u WHERE u.id = ? AND u.status = ? AND u.is_active = ?)", actorID, "1", true).
			Where("EXISTS (SELECT 1 FROM t_ai_tenants t WHERE t.id = ? AND t.status = ?)", actorID, "1").Take(&parent).Error; err != nil {
			return err
		}
		if ids == nil && deleteAll {
			if err := tx.Model(&entity.API4Conversation{}).Where("dialog_id = ? AND source = ?", agentID, "agent").Order("id").Pluck("id", &ids).Error; err != nil {
				return err
			}
		}
		for _, id := range ids {
			result := tx.Where("id = ? AND dialog_id = ? AND source = ?", id, agentID, "agent").Delete(&entity.API4Conversation{})
			if result.Error != nil {
				return result.Error
			}
			if result.RowsAffected == 0 {
				missing = append(missing, "The agent doesn't own the session "+id)
			} else {
				count++
			}
		}
		return nil
	})
	if err != nil {
		return 0, nil, err
	}
	return count, missing, nil
}
