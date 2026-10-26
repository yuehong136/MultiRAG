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
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"strings"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
)

const (
	mcpServerTypeSSE            = "sse"
	mcpServerTypeStreamableHTTP = "streamable-http"
	mcpServerNameLimit          = 255
)

// MCPService handles MCP server operations.
type MCPService struct {
	mcpServerDAO *dao.MCPServerDAO
}

// NewMCPService creates an MCP service.
func NewMCPService() *MCPService {
	return &MCPService{
		mcpServerDAO: dao.NewMCPServerDAO(),
	}
}

// CreateMCPServerRequest is the request payload for creating an MCP server.
type CreateMCPServerRequest struct {
	Name        string          `json:"name"`
	URL         string          `json:"url"`
	ServerType  string          `json:"server_type"`
	Description *string         `json:"description,omitempty"`
	Variables   json.RawMessage `json:"variables,omitempty"`
	Headers     json.RawMessage `json:"headers,omitempty"`
}

// CreateMCPServerResponse is the response payload for creating an MCP server.
type CreateMCPServerResponse struct {
	ID          string         `json:"id"`
	TenantID    string         `json:"tenant_id"`
	Name        string         `json:"name"`
	URL         string         `json:"url"`
	ServerType  string         `json:"server_type"`
	Description *string        `json:"description,omitempty"`
	Variables   entity.JSONMap `json:"variables"`
	Headers     entity.JSONMap `json:"headers"`
}

// CreateMCPServer creates an MCP server owned by a tenant.
func (s *MCPService) CreateMCPServer(ctx context.Context, tenantID string, req CreateMCPServerRequest) (*CreateMCPServerResponse, common.ErrorCode, error) {
	if !isValidMCPServerType(req.ServerType) {
		return nil, common.CodeDataError, errors.New("Unsupported MCP server type.")
	}

	if strings.TrimSpace(req.Name) == "" || len([]byte(req.Name)) > mcpServerNameLimit {
		return nil, common.CodeDataError, fmt.Errorf("Invalid MCP name or length is %d which is large than 255.", len([]byte(req.Name)))
	}

	if tenantID == "" {
		return nil, common.CodeAuthenticationError, errors.New("No authorization.")
	}
	parsed, err := url.Parse(req.URL)
	if err != nil || (parsed.Scheme != "https" && parsed.Scheme != "http") || parsed.Hostname() == "" || parsed.User != nil || parsed.Fragment != "" || strings.TrimSpace(req.URL) != req.URL {
		return nil, common.CodeArgumentError, errors.New("Invalid url.")
	}
	headers, err := mcpJSONMap(req.Headers)
	if err != nil {
		return nil, common.CodeArgumentError, err
	}
	variables, err := mcpJSONMap(req.Variables)
	if err != nil {
		return nil, common.CodeArgumentError, err
	}
	for key, value := range headers {
		v, ok := value.(string)
		if !ok || key == "" || strings.ContainsAny(key+v, "\r\n") {
			return nil, common.CodeArgumentError, errors.New("Invalid MCP headers.")
		}
	}
	variables["tools"] = map[string]interface{}{}
	server := &entity.MCPServer{
		ID:          common.GenerateUUID(),
		Name:        req.Name,
		TenantID:    tenantID,
		URL:         req.URL,
		ServerType:  req.ServerType,
		Description: req.Description,
		Variables:   variables,
		Headers:     headers,
	}

	err = s.mcpServerDAO.CreateMCPServer(ctx, server)
	if err != nil {
		return nil, common.CodeDataError, err
	}

	return &CreateMCPServerResponse{
		ID:          server.ID,
		TenantID:    server.TenantID,
		Name:        server.Name,
		URL:         server.URL,
		ServerType:  server.ServerType,
		Description: server.Description,
		Variables:   entity.JSONMap{"tools": map[string]interface{}{}},
		Headers:     entity.JSONMap{},
	}, common.CodeSuccess, nil
}

func isValidMCPServerType(serverType string) bool {
	return serverType == mcpServerTypeSSE || serverType == mcpServerTypeStreamableHTTP
}

func mcpJSONMap(raw json.RawMessage) (entity.JSONMap, error) {
	raw = bytes.TrimSpace(raw)
	if len(raw) == 0 {
		return entity.JSONMap{}, nil
	}
	var text string
	if json.Unmarshal(raw, &text) == nil {
		raw = []byte(text)
	}
	var value entity.JSONMap
	if json.Unmarshal(raw, &value) != nil || value == nil {
		return nil, errors.New("MCP settings must be JSON objects.")
	}
	return value, nil
}

// DeleteMCPServer refuses deletion while a saved canvas or version references it.
// MCP configuration is SQL-only; it does not own remote server data or storage objects.
func (s *MCPService) DeleteMCPServer(ctx context.Context, tenantID, mcpID string) (bool, common.ErrorCode, error) {
	if tenantID == "" {
		return false, common.CodeAuthenticationError, errors.New("No authorization.")
	}
	if strings.TrimSpace(mcpID) == "" {
		return false, common.CodeArgumentError, errors.New("mcp_id is required")
	}
	code := common.CodeServerError
	err := dao.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var tenant entity.Tenant
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ?", tenantID).Take(&tenant).Error; err != nil {
			return err
		}
		var row entity.MCPServer
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ? AND tenant_id = ?", mcpID, tenantID).Take(&row).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				code = common.CodeDataError
			}
			return err
		}
		for _, table := range []string{"t_ai_user_canvases", "t_ai_user_canvas_version"} {
			var count int64
			// JSONPath variables keep user input out of SQL and match exact mcp_id values.
			err := tx.Table(table).Where("jsonb_path_exists(dsl, CAST(? AS jsonpath), jsonb_build_object('id', CAST(? AS text)))", "$.**.mcp_id ? (@ == $id)", mcpID).Count(&count).Error
			if err != nil {
				return err
			}
			if count != 0 {
				code = common.CodeOperatingError
				return errors.New("MCP server is referenced by a saved agent.")
			}
		}
		result := tx.Where("id = ? AND tenant_id = ?", mcpID, tenantID).Delete(&entity.MCPServer{})
		if result.Error != nil {
			return result.Error
		}
		if result.RowsAffected != 1 {
			code = common.CodeDataError
			return gorm.ErrRecordNotFound
		}
		return nil
	})
	if err != nil {
		if code == common.CodeOperatingError {
			return false, code, errors.New("MCP server is referenced by a saved agent.")
		}
		return false, code, errors.New("Unable to delete MCP server.")
	}
	return true, common.CodeSuccess, nil
}
