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

package handler

import (
	"encoding/json"
	"errors"
	"math"
	"multirag/internal/dao"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"

	"multirag/internal/common"
	"multirag/internal/service"
)

// MCPHandler handles MCP server requests.
type MCPHandler struct {
	mcpService *service.MCPService
}

// NewMCPHandler creates an MCP handler.
func NewMCPHandler(mcpService *service.MCPService) *MCPHandler {
	return &MCPHandler{
		mcpService: mcpService,
	}
}

// CreateMCPServer creates an MCP server for the current user.
func (h *MCPHandler) CreateMCPServer(c *gin.Context) {
	user, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}

	var req service.CreateMCPServerRequest
	if err := bindObjectJSON(c, &req); err != nil {
		jsonError(c, common.CodeArgumentError, "Invalid MCP payload.")
		return
	}

	result, code, err := h.mcpService.CreateMCPServer(c.Request.Context(), user.ID, req)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    common.CodeSuccess,
		"message": "success",
		"data":    result,
	})
}

// DeleteMCPServer deletes only an owned, unreferenced configuration.
func (h *MCPHandler) DeleteMCPServer(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	result, code, err := h.mcpService.DeleteMCPServer(c.Request.Context(), user.ID, c.Param("mcp_id"))
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, code, result, "success")
}

func parseMCPListQuery(query url.Values) (dao.MCPListQuery, string, error) {
	opts := dao.MCPListQuery{OrderBy: "create_time", Desc: true}
	invalid := errors.New("Invalid MCP list query.")
	for key, values := range query {
		if key != "mcp_ids" && len(values) != 1 {
			return opts, "", invalid
		}
	}
	for key, target := range map[string]*int{"page": &opts.Page, "page_size": &opts.PageSize} {
		if raw, ok := query[key]; ok {
			if raw[0] == "" || strings.Trim(raw[0], "0123456789") != "" {
				return opts, "", invalid
			}
			value, err := strconv.Atoi(raw[0])
			if err != nil {
				return opts, "", invalid
			}
			*target = value
		}
	}
	if opts.PageSize > 100 || (opts.PageSize > 0 && opts.Page > math.MaxInt/opts.PageSize) {
		return opts, "", invalid
	}
	if raw, ok := query["orderby"]; ok {
		opts.OrderBy = raw[0]
	}
	if _, err := dao.MCPServerOrderColumn(opts.OrderBy); err != nil {
		return opts, "", invalid
	}
	if raw, ok := query["desc"]; ok {
		switch raw[0] {
		case "true":
			opts.Desc = true
		case "false":
			opts.Desc = false
		default:
			return opts, "", invalid
		}
	}
	opts.Keywords = query.Get("keywords")
	values, plural := query["mcp_ids"]
	if singular, ok := query["mcp_id"]; ok {
		if plural {
			return opts, "", invalid
		}
		values = singular
	}
	if values != nil {
		opts.IDs = []string{}
		seen := map[string]bool{}
		for _, raw := range values {
			if raw == "null" || (raw == "[]" && len(values) != 1) {
				return opts, "", invalid
			}
			var ids []string
			if strings.HasPrefix(raw, "[") {
				if json.Unmarshal([]byte(raw), &ids) != nil || ids == nil {
					return opts, "", invalid
				}
			} else {
				ids = strings.Split(raw, ",")
			}
			for _, id := range ids {
				if id == "" || strings.TrimSpace(id) != id || strings.ContainsAny(id, "[]{}\" \t\r\n") || seen[id] {
					return opts, "", invalid
				}
				seen[id] = true
				opts.IDs = append(opts.IDs, id)
			}
		}
	}
	tenant := query.Get("tenant_id")
	if _, ok := query["tenant_id"]; ok && (tenant == "" || strings.TrimSpace(tenant) != tenant) {
		return opts, "", invalid
	}
	return opts, tenant, nil
}

// ListMCPServers lists management configuration for an authorized tenant owner.
func (h *MCPHandler) ListMCPServers(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	query, err := url.ParseQuery(c.Request.URL.RawQuery)
	if err != nil {
		jsonError(c, common.CodeArgumentError, "Invalid MCP list query.")
		return
	}
	opts, tenant, err := parseMCPListQuery(query)
	if err != nil {
		jsonError(c, common.CodeArgumentError, err.Error())
		return
	}
	if tenant == "" {
		tenant = user.ID
	}
	result, code, err := h.mcpService.ListMCPServers(c.Request.Context(), user.ID, tenant, opts)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, result, "success")
}

// UpdateMCPServer changes explicit settings and returns a credential-free view.
func (h *MCPHandler) UpdateMCPServer(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	var req service.UpdateMCPServerRequest
	if bindObjectJSONNullable(c, &req, map[string]bool{"description": true}) != nil {
		jsonError(c, common.CodeArgumentError, "Invalid MCP payload.")
		return
	}
	result, code, err := h.mcpService.UpdateMCPServer(c.Request.Context(), user.ID, c.Param("mcp_id"), req)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, code, result, "success")
}
