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

package service

import (
	"context"
	"errors"
	"fmt"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/entity"
	"strings"
	"unicode/utf8"

	"gorm.io/gorm"
)

// ConnectorService connector service
type ConnectorService struct {
	oauth        *ConnectorOAuthService
	connectorDAO *dao.ConnectorDAO
	docEngine    engine.DocEngine
}

// NewConnectorService create connector service
func NewConnectorService(stores ...engine.DocEngine) *ConnectorService {
	store := engine.Get()
	if len(stores) > 0 {
		store = stores[0]
	}
	return &ConnectorService{
		connectorDAO: dao.NewConnectorDAO(),
		docEngine:    store,
	}
}

// ListConnectorsResponse list connectors response
type ListConnectorsResponse struct {
	Connectors []*dao.ConnectorListItem `json:"connectors"`
}

// ListConnectors list connectors for a user
// Equivalent to Python's ConnectorService.list(current_user.id)
func (s *ConnectorService) ListConnectors(userID string) (*ListConnectorsResponse, error) {
	if userID == "" {
		return nil, fmt.Errorf("No authorization.")
	}
	connectors, err := s.connectorDAO.ListAccessible(userID)
	if err != nil {
		return nil, fmt.Errorf("Unable to list connectors.")
	}
	return &ListConnectorsResponse{Connectors: connectors}, nil
}

// GetConnector returns safe metadata under the current tenant resource access policy.
func (s *ConnectorService) GetConnector(ctx context.Context, connectorID, userID string) (*dao.ConnectorDetail, common.ErrorCode, error) {
	if connectorID == "" {
		return nil, common.CodeDataError, fmt.Errorf("connector_id is required")
	}
	if userID == "" {
		return nil, common.CodeAuthenticationError, fmt.Errorf("No authorization.")
	}
	detail, err := s.connectorDAO.GetAccessibleDetail(ctx, connectorID, userID)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, common.CodeAuthenticationError, fmt.Errorf("No authorization.")
	}
	if err != nil {
		return nil, common.CodeServerError, fmt.Errorf("Unable to read connector.")
	}
	if detail.IndexingStart != nil {
		timestamp := detail.IndexingStart.UTC()
		detail.IndexingStart = &timestamp
	}
	return detail, common.CodeSuccess, nil
}

// CreateConnectorRequest uses pointers to distinguish omitted frequencies from legal zero.
type CreateConnectorRequest struct {
	Name        string         `json:"name"`
	Source      string         `json:"source"`
	Config      entity.JSONMap `json:"config"`
	RefreshFreq *int64         `json:"refresh_freq,omitempty"`
	PruneFreq   *int64         `json:"prune_freq,omitempty"`
	TimeoutSecs *int64         `json:"timeout_secs,omitempty"`
}

// CreateConnector persists configuration without contacting a source or scheduling work.
func (s *ConnectorService) CreateConnector(ctx context.Context, userID string, req *CreateConnectorRequest) (*dao.ConnectorDetail, common.ErrorCode, error) {
	if userID == "" {
		return nil, common.CodeAuthenticationError, fmt.Errorf("No authorization.")
	}
	if req == nil || strings.TrimSpace(req.Name) == "" || !utf8.ValidString(req.Name) || utf8.RuneCountInString(req.Name) > 128 || strings.TrimSpace(req.Source) == "" || !utf8.ValidString(req.Source) || utf8.RuneCountInString(req.Source) > 128 || req.Config == nil {
		return nil, common.CodeArgumentError, fmt.Errorf("name, source and config are required")
	}
	refresh, prune, timeout := int64(5), int64(5), int64(1740)
	if req.RefreshFreq != nil {
		refresh = *req.RefreshFreq
	}
	if req.PruneFreq != nil {
		prune = *req.PruneFreq
	}
	if req.TimeoutSecs != nil {
		timeout = *req.TimeoutSecs
	}
	if refresh < 0 || prune < 0 || timeout <= 0 {
		return nil, common.CodeArgumentError, fmt.Errorf("invalid connector interval or timeout")
	}
	connector := &entity.Connector{ID: common.GenerateUUID(), TenantID: userID, Name: req.Name, Source: req.Source, Config: req.Config, InputType: "poll", RefreshFreq: refresh, PruneFreq: prune, TimeoutSecs: timeout, Status: "0"}
	if err := dao.DB.WithContext(ctx).Create(connector).Error; err != nil {
		return nil, common.CodeServerError, fmt.Errorf("Unable to create connector.")
	}
	return &dao.ConnectorDetail{ID: connector.ID, TenantID: userID, Name: connector.Name, Source: connector.Source, InputType: connector.InputType, RefreshFreq: refresh, PruneFreq: prune, TimeoutSecs: timeout, Status: connector.Status}, common.CodeSuccess, nil
}
