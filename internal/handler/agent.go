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
	"net/http"
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"

	"multirag/internal/common"
	"multirag/internal/service"
)

// AgentHandler agent handler
type AgentHandler struct {
	agentService *service.AgentService
}

// NewAgentHandler create agent handler
func NewAgentHandler(agentService *service.AgentService) *AgentHandler {
	return &AgentHandler{agentService: agentService}
}

// ListAgents lists agent canvases for the current user.
// @Summary List Agents
// @Description List agent canvases accessible to the current user (Home dashboard tile)
// @Tags agents
// @Produce json
// @Param keywords query string false "Filter by title keyword"
// @Param page query int false "Page number (0 = no pagination)"
// @Param page_size query int false "Items per page (0 = no pagination)"
// @Param orderby query string false "Order-by field (default: create_time)"
// @Param desc query bool false "Descending order (default: true)"
// @Param owner_ids query string false "Comma-separated owner IDs to filter (default: all authorised tenants)"
// @Param canvas_category query string false "Canvas category (default: agent_canvas)"
// @Success 200 {object} service.ListAgentsResponse
// @Router /api/v1/agents [get]
func (h *AgentHandler) ListAgents(c *gin.Context) {
	user, errorCode, errorMessage := GetUser(c)
	if errorCode != common.CodeSuccess {
		jsonError(c, errorCode, errorMessage)
		return
	}

	for _, key := range []string{"page", "page_size", "orderby", "desc", "owner_ids", "canvas_category", "keywords"} {
		if values, ok := c.Request.URL.Query()[key]; ok && (len(values) != 1 || (key != "owner_ids" && key != "keywords" && strings.TrimSpace(values[0]) == "")) {
			jsonError(c, common.CodeArgumentError, "invalid query parameter")
			return
		}
	}
	keywords, canvasCategory := c.Query("keywords"), c.Query("canvas_category")
	page, pageSize := 0, 0
	for key, dest := range map[string]*int{"page": &page, "page_size": &pageSize} {
		if raw, ok := c.GetQuery(key); ok {
			value, err := strconv.Atoi(raw)
			if err != nil {
				jsonError(c, common.CodeArgumentError, "invalid pagination")
				return
			}
			*dest = value
		}
	}
	orderby := c.DefaultQuery("orderby", "create_time")
	desc := true
	if raw, ok := c.GetQuery("desc"); ok {
		if raw != "true" && raw != "false" {
			jsonError(c, common.CodeArgumentError, "invalid desc")
			return
		}
		desc = raw == "true"
	}
	var ownerIDs []string
	if values, ok := c.Request.URL.Query()["owner_ids"]; ok {
		ownerIDs = []string{}
		if values[0] != "" {
			seen := map[string]bool{}
			for _, id := range strings.Split(values[0], ",") {
				id = strings.TrimSpace(id)
				if id == "" || seen[id] {
					jsonError(c, common.CodeArgumentError, "invalid owner_ids")
					return
				}
				seen[id] = true
				ownerIDs = append(ownerIDs, id)
			}
		}
	}

	result, code, err := h.agentService.ListAgents(
		c.Request.Context(),
		user.ID,
		keywords,
		page,
		pageSize,
		orderby,
		desc,
		ownerIDs,
		canvasCategory,
	)
	if err != nil {
		c.JSON(http.StatusOK, gin.H{
			"code":    code,
			"data":    false,
			"message": err.Error(),
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    common.CodeSuccess,
		"data":    result,
		"message": "success",
	})
}
