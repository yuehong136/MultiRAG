package service

import (
	"context"
	"encoding/json"
	"errors"
	"net/url"
	"strings"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
)

type UpdateMCPServerRequest struct {
	Name        *string         `json:"name"`
	URL         *string         `json:"url"`
	ServerType  *string         `json:"server_type"`
	Description json.RawMessage `json:"description"`
	Headers     json.RawMessage `json:"headers"`
	Variables   json.RawMessage `json:"variables"`
}

func (s *MCPService) UpdateMCPServer(ctx context.Context, userID, id string, req UpdateMCPServerRequest) (*MCPServerListItem, common.ErrorCode, error) {
	updates := map[string]any{}
	if strings.TrimSpace(id) == "" {
		return nil, common.CodeArgumentError, errors.New("MCP ID is required.")
	}
	if req.Name != nil {
		if strings.TrimSpace(*req.Name) == "" || len([]byte(*req.Name)) > mcpServerNameLimit {
			return nil, common.CodeArgumentError, errors.New("Invalid MCP name.")
		}
		updates["name"] = *req.Name
	}
	if req.URL != nil {
		endpoint, err := url.Parse(*req.URL)
		if err != nil || (endpoint.Scheme != "http" && endpoint.Scheme != "https") || endpoint.Hostname() == "" || endpoint.User != nil || endpoint.Fragment != "" || strings.TrimSpace(*req.URL) != *req.URL {
			return nil, common.CodeArgumentError, errors.New("Invalid MCP endpoint.")
		}
		updates["url"] = *req.URL
	}
	if req.ServerType != nil {
		if !isValidMCPServerType(*req.ServerType) {
			return nil, common.CodeArgumentError, errors.New("Unsupported MCP server type.")
		}
		updates["server_type"] = *req.ServerType
	}
	if req.Description != nil {
		var desc *string
		if json.Unmarshal(req.Description, &desc) != nil {
			return nil, common.CodeArgumentError, errors.New("Invalid MCP description.")
		}
		updates["description"] = desc
	}
	for key, raw := range map[string]json.RawMessage{"headers": req.Headers, "variables": req.Variables} {
		if raw == nil {
			continue
		}
		var value entity.JSONMap
		if json.Unmarshal(raw, &value) != nil || value == nil {
			return nil, common.CodeArgumentError, errors.New("MCP settings must be JSON objects.")
		}
		if key == "headers" {
			for name, item := range value {
				text, ok := item.(string)
				if !ok || name == "" || strings.ContainsAny(name+text, "\r\n") {
					return nil, common.CodeArgumentError, errors.New("Invalid MCP headers.")
				}
			}
		} else {
			value["tools"] = map[string]any{}
		}
		updates[key] = value
	}
	var row entity.MCPServer
	err := dao.DB.WithContext(ctx).Session(&gorm.Session{Logger: logger.Discard}).Transaction(func(tx *gorm.DB) error {
		if err := verifyTenantOwner(tx, userID, userID); err != nil {
			return err
		}
		var tenant entity.Tenant
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ? AND status = ?", userID, "1").Take(&tenant).Error; err != nil {
			return errTenantMemberDenied
		}
		// The display projection never SELECTs saved headers or variables.
		if err := tx.Select("id", "name", "url", "tenant_id", "server_type", "description", "create_date", "update_date").Clauses(clause.Locking{Strength: "UPDATE"}).Where("id = ? AND tenant_id = ?", id, userID).Take(&row).Error; err != nil {
			return err
		}
		if req.Name != nil {
			var count int64
			if err := tx.Model(&entity.MCPServer{}).Where("tenant_id = ? AND name = ? AND id <> ?", userID, *req.Name, id).Count(&count).Error; err != nil {
				return err
			}
			if count != 0 {
				return errMCPDuplicateName
			}
		}
		if len(updates) > 0 {
			if err := tx.Model(&entity.MCPServer{}).Where("id = ? AND tenant_id = ?", id, userID).Updates(updates).Error; err != nil {
				return err
			}
		}
		return tx.Select("id", "name", "url", "tenant_id", "server_type", "description", "create_date", "update_date").Where("id = ? AND tenant_id = ?", id, userID).Take(&row).Error
	})
	if errors.Is(err, errTenantMemberDenied) || errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, common.CodeForbidden, errors.New("MCP server unavailable.")
	}
	if errors.Is(err, errMCPDuplicateName) {
		return nil, common.CodeArgumentError, errMCPDuplicateName
	}
	if err != nil {
		return nil, common.CodeServerError, errors.New("Unable to update MCP server.")
	}
	endpoint, err := url.Parse(row.URL)
	if err != nil {
		return nil, common.CodeServerError, errors.New("Invalid saved MCP endpoint.")
	}
	endpoint.RawQuery = ""
	endpoint.Fragment = ""
	endpoint.User = nil
	return &MCPServerListItem{ID: row.ID, Name: row.Name, URL: endpoint.String(), ServerType: row.ServerType, Description: row.Description, Variables: entity.JSONMap{"tools": map[string]any{}}, CreateDate: row.CreateDate, UpdateDate: row.UpdateDate}, common.CodeSuccess, nil
}

var errMCPDuplicateName = errors.New("Duplicated MCP server name.")
