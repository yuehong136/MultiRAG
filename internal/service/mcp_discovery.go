package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/utility"
)

// MCPDiscoveryRequest describes an unsaved, read-only remote discovery.
// Timeout is seconds, including initialization and all tool-list pages.
type MCPDiscoveryRequest struct {
	URL        string         `json:"url"`
	ServerType string         `json:"server_type"`
	Headers    MCPHeaders     `json:"headers"`
	Variables  entity.JSONMap `json:"variables"`
	Timeout    float64        `json:"timeout"`
}

type ImportMCPRequest struct {
	Servers map[string]json.RawMessage `json:"mcpServers"`
	Timeout float64                    `json:"timeout"`
}

type MCPImportResult struct {
	Server  string `json:"server"`
	Success bool   `json:"success"`
	Action  string `json:"action,omitempty"`
	ID      string `json:"id,omitempty"`
	NewName string `json:"new_name,omitempty"`
	Message string `json:"message,omitempty"`
}

func mcpDiscoveryTimeout(seconds float64) (time.Duration, error) {
	if seconds == 0 {
		return 10 * time.Second, nil
	}
	if math.IsNaN(seconds) || math.IsInf(seconds, 0) || seconds < 0 || seconds > 60 {
		return 0, errors.New("MCP timeout must be between 0 and 60 seconds.")
	}
	return time.Duration(seconds * float64(time.Second)), nil
}

// authorizeMCPManagement follows the existing personal-tenant owner boundary.
// It precedes every remote connection and is repeated under lock before writes.
func authorizeMCPManagement(tx *gorm.DB, userID string, lock bool) error {
	if userID == "" {
		return errTenantMemberDenied
	}
	var tenant entity.Tenant
	q := tx.Where("id = ? AND status = ?", userID, "1")
	if lock {
		q = q.Clauses(clause.Locking{Strength: "UPDATE"})
	}
	if err := q.Take(&tenant).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return errTenantMemberDenied
		}
		return err
	}
	if err := verifyTenantOwner(tx, userID, userID); err != nil {
		return err
	}
	var count int64
	if err := tx.Model(&entity.User{}).Where("id = ? AND status = ? AND is_active = ? AND is_authenticated = ?", userID, "1", true, true).Count(&count).Error; err != nil {
		return err
	}
	if count != 1 {
		return errTenantMemberDenied
	}
	return nil
}

func mcpDiscoveryCode(err error) common.ErrorCode {
	if errors.Is(err, errTenantMemberDenied) || errors.Is(err, gorm.ErrRecordNotFound) {
		return common.CodeForbidden
	}
	return common.CodeServerError
}

func (s *MCPService) discoverMCP(ctx context.Context, req MCPDiscoveryRequest) ([]utility.Tool, error) {
	timeout, err := mcpDiscoveryTimeout(req.Timeout)
	if err != nil {
		return nil, err
	}
	if !isValidMCPServerType(req.ServerType) {
		return nil, errors.New("Unsupported MCP server type.")
	}
	vars, _ := mcpStringMap(req.Variables, true)
	return s.fetchMCPTools(ctx, utility.FetchOptions{URL: req.URL, ServerType: req.ServerType, Headers: req.Headers, Variables: vars, Timeout: timeout})
}

// TestMCPServer never saves the supplied endpoint, credentials or discovered tools.
func (s *MCPService) TestMCPServer(ctx context.Context, userID string, req MCPDiscoveryRequest) ([]map[string]any, common.ErrorCode, error) {
	if _, err := mcpDiscoveryTimeout(req.Timeout); err != nil {
		return nil, common.CodeArgumentError, err
	}
	if !isValidMCPServerType(req.ServerType) || req.URL == "" {
		return nil, common.CodeArgumentError, errors.New("Invalid MCP discovery settings.")
	}
	err := dao.DB.WithContext(ctx).Session(&gorm.Session{Logger: logger.Discard}).Transaction(func(tx *gorm.DB) error { return authorizeMCPManagement(tx, userID, false) })
	if err != nil {
		return nil, mcpDiscoveryCode(err), errors.New("MCP server unavailable.")
	}
	tools, err := s.discoverMCP(ctx, req)
	if err != nil {
		return nil, common.CodeDataError, errors.New("MCP discovery failed.")
	}
	return mcpToolViews(tools), common.CodeSuccess, nil
}

// GetMCPServerTools uses only an owned stored configuration. Discovery is read-only.
func (s *MCPService) GetMCPServerTools(ctx context.Context, userID, id string, seconds float64) ([]map[string]any, common.ErrorCode, error) {
	if _, err := mcpDiscoveryTimeout(seconds); err != nil {
		return nil, common.CodeArgumentError, err
	}
	var row entity.MCPServer
	err := dao.DB.WithContext(ctx).Session(&gorm.Session{Logger: logger.Discard}).Transaction(func(tx *gorm.DB) error {
		if err := authorizeMCPManagement(tx, userID, false); err != nil {
			return err
		}
		return tx.Where("id = ? AND tenant_id = ?", id, userID).Take(&row).Error
	})
	if err != nil {
		return nil, mcpDiscoveryCode(err), errors.New("MCP server unavailable.")
	}
	headers, err := mcpStringMap(row.Headers, false)
	if err != nil {
		return nil, common.CodeDataError, errors.New("Invalid saved MCP settings.")
	}

	tools, err := s.discoverMCP(ctx, MCPDiscoveryRequest{URL: row.URL, ServerType: row.ServerType, Headers: headers, Variables: row.Variables, Timeout: seconds})
	if err != nil {
		return nil, common.CodeDataError, errors.New("MCP discovery failed.")
	}
	views := mcpToolViews(tools)
	if saved, ok := row.Variables["tools"].(map[string]any); ok {
		for _, tool := range views {
			if previous, ok := saved[tool["name"].(string)].(map[string]any); ok {
				if enabled, ok := previous["enabled"].(bool); ok {
					tool["enabled"] = enabled
				}
			}
		}
	}
	return views, common.CodeSuccess, nil
}

func mcpStringMap(value map[string]any, skipTools bool) (map[string]string, error) {
	out := map[string]string{}
	for k, v := range value {
		if skipTools && k == "tools" {
			continue
		}
		s, ok := v.(string)
		if !ok {
			if !skipTools {
				return nil, errors.New("MCP settings must contain strings.")
			}
			// Python Template renders JSON scalars as text. Keep arbitrary
			// object metadata intact without interpreting it as credentials.
			switch value := v.(type) {
			case float64, float32, int, int64, json.Number:
				s = fmt.Sprint(value)
			case bool:
				if value {
					s = "True"
				} else {
					s = "False"
				}
			case nil:
				s = "None"
			default:
				continue
			}
		}
		out[k] = s
	}
	return out, nil
}

func mcpToolViews(tools []utility.Tool) []map[string]any {
	out := make([]map[string]any, 0, len(tools))
	for _, tool := range tools {
		// Project only standard discovery fields. Arbitrary remote extension fields
		// are untrusted and may reflect connection credentials.
		item := map[string]any{"name": tool.Name, "enabled": true}
		if tool.Description != "" {
			item["description"] = tool.Description
		}
		if tool.InputSchema != nil {
			item["inputSchema"] = tool.InputSchema
		}
		out = append(out, item)
	}
	return out
}

func decodeMCPImport(name string, raw json.RawMessage, timeout float64) (MCPDiscoveryRequest, entity.JSONMap, error) {
	req := MCPDiscoveryRequest{Timeout: timeout, Headers: map[string]string{}, Variables: entity.JSONMap{}}
	if strings.TrimSpace(name) == "" || strings.TrimSpace(name) != name || len([]byte(name)) > mcpServerNameLimit {
		return req, nil, errors.New("Invalid MCP name.")
	}
	var cfg map[string]json.RawMessage
	if json.Unmarshal(raw, &cfg) != nil || cfg == nil {
		return req, nil, errors.New("Invalid MCP config.")
	}
	if json.Unmarshal(cfg["type"], &req.ServerType) != nil || json.Unmarshal(cfg["url"], &req.URL) != nil || req.URL == "" || !isValidMCPServerType(req.ServerType) {
		return req, nil, errors.New("Invalid MCP discovery settings.")
	}
	if value, ok := cfg["headers"]; ok {
		if json.Unmarshal(value, &req.Headers) != nil || req.Headers == nil {
			return req, nil, errors.New("Invalid MCP headers.")
		}
	}
	variables := entity.JSONMap{}
	for key, value := range cfg {
		if key == "url" || key == "type" || key == "headers" || key == "tools" {
			continue
		}
		var valueJSON any
		if json.Unmarshal(value, &valueJSON) != nil {
			return req, nil, errors.New("Invalid MCP variable.")
		}
		variables[key] = valueJSON
		req.Variables[key] = valueJSON
		if key == "authorization_token" {
			text, ok := valueJSON.(string)
			if !ok {
				return req, nil, errors.New("Invalid MCP authorization token.")
			}
			if _, ok := req.Headers[key]; !ok {
				req.Headers[key] = text
			}
		}
	}
	return req, variables, nil
}

// ImportMCPServers reports each entry separately, matching the production import
// contract. Each successful entry commits atomically; failed entries write nothing.
func (s *MCPService) ImportMCPServers(ctx context.Context, userID string, req ImportMCPRequest) ([]MCPImportResult, common.ErrorCode, error) {
	if _, err := mcpDiscoveryTimeout(req.Timeout); err != nil {
		return nil, common.CodeArgumentError, err
	}
	if len(req.Servers) == 0 || len(req.Servers) > 32 {
		return nil, common.CodeArgumentError, errors.New("Provide between 1 and 32 MCP servers.")
	}
	err := dao.DB.WithContext(ctx).Session(&gorm.Session{Logger: logger.Discard}).Transaction(func(tx *gorm.DB) error { return authorizeMCPManagement(tx, userID, false) })
	if err != nil {
		return nil, mcpDiscoveryCode(err), errors.New("MCP server unavailable.")
	}
	names := make([]string, 0, len(req.Servers))
	for name := range req.Servers {
		names = append(names, name)
	}
	sort.Strings(names)
	results := make([]MCPImportResult, 0, len(names))
	for _, name := range names {
		if ctx.Err() != nil {
			return results, common.CodeDataError, errors.New("MCP import cancelled.")
		}
		authErr := dao.DB.WithContext(ctx).Session(&gorm.Session{Logger: logger.Discard}).Transaction(func(tx *gorm.DB) error { return authorizeMCPManagement(tx, userID, false) })
		if authErr != nil {
			return results, mcpDiscoveryCode(authErr), errors.New("MCP server unavailable.")
		}
		result := MCPImportResult{Server: name}
		candidate, vars, err := decodeMCPImport(name, req.Servers[name], req.Timeout)
		if err != nil {
			result.Message = err.Error()
			results = append(results, result)
			continue
		}
		tools, err := s.discoverMCP(ctx, candidate)
		if err != nil {
			if ctx.Err() != nil {
				return results, common.CodeDataError, errors.New("MCP import cancelled.")
			}
			result.Message = "MCP discovery failed."
			results = append(results, result)
			continue
		}
		toolMap := map[string]any{}
		for _, tool := range mcpToolViews(tools) {
			toolMap[tool["name"].(string)] = tool
		}
		vars["tools"] = toolMap
		headers := entity.JSONMap{}
		for k, v := range candidate.Headers {
			headers[k] = v
		}
		row := entity.MCPServer{ID: common.GenerateUUID(), TenantID: userID, URL: candidate.URL, ServerType: candidate.ServerType, Variables: vars, Headers: headers}
		err = dao.DB.WithContext(ctx).Session(&gorm.Session{Logger: logger.Discard}).Transaction(func(tx *gorm.DB) error {
			if err := authorizeMCPManagement(tx, userID, true); err != nil {
				return err
			}
			// The existing tenant lock also serializes Create/Update duplicate checks.
			for suffix := -1; suffix < 10000; suffix++ {
				row.Name = name
				if suffix >= 0 {
					row.Name = fmt.Sprintf("%s_%d", name, suffix)
				}
				if len([]byte(row.Name)) > mcpServerNameLimit {
					return errors.New("MCP name conflict.")
				}
				var count int64
				if err := tx.Model(&entity.MCPServer{}).Where("tenant_id = ? AND name = ?", userID, row.Name).Count(&count).Error; err != nil {
					return err
				}
				if count != 0 {
					continue
				}
				return tx.Create(&row).Error
			}
			return errors.New("MCP name conflict.")
		})
		if errors.Is(err, errTenantMemberDenied) {
			return results, common.CodeForbidden, errors.New("MCP server unavailable.")
		}
		if err != nil {
			result.Message = "Unable to import MCP server."
		} else {
			result.Success = true
			result.Action = "created"
			result.ID = row.ID
			result.NewName = row.Name
		}
		results = append(results, result)
		if ctx.Err() != nil {
			return results, common.CodeDataError, errors.New("MCP import cancelled.")
		}
	}
	return results, common.CodeSuccess, nil
}
