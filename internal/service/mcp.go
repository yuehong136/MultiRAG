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
