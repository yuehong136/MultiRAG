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
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"

	"multirag/internal/common"
	"multirag/internal/service"
)

// AgentHandler agent handler
type AgentHandler struct {
	agentService *service.AgentService
	loadPrompt   func(string) (string, error)
}

// NewAgentHandler create agent handler
func NewAgentHandler(agentService *service.AgentService) *AgentHandler {
	return &AgentHandler{agentService: agentService}
}

// ListTemplates keeps the catalogue's multilingual objects and DSL intact.
func (h *AgentHandler) ListTemplates(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	rows, code, err := h.agentService.ListTemplates(c.Request.Context(), user.ID)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, rows, "success")
}

// UpdateAgentTags requires a present string or array of strings; [] clears tags.
func (h *AgentHandler) UpdateAgentTags(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonResponse(c, code, false, message)
		return
	}
	var request map[string]json.RawMessage
	if err := bindObjectJSON(c, &request); err != nil || len(request) != 1 || len(request["tags"]) == 0 {
		jsonResponse(c, common.CodeArgumentError, false, "tags must be a string or array of strings")
		return
	}
	var tags any
	var text string
	if err := json.Unmarshal(request["tags"], &text); err == nil {
		tags = text
	} else {
		var values []json.RawMessage
		if err = json.Unmarshal(request["tags"], &values); err != nil {
			jsonResponse(c, common.CodeArgumentError, false, "tags must be a string or array of strings")
			return
		}
		stringsOnly := make([]string, len(values))
		for i, value := range values {
			if strings.TrimSpace(string(value)) == "null" || json.Unmarshal(value, &stringsOnly[i]) != nil {
				jsonResponse(c, common.CodeArgumentError, false, "tags must be a string or array of strings")
				return
			}
		}
		tags = stringsOnly
	}
	updated, code, err := h.agentService.UpdateAgentTags(c.Request.Context(), user.ID, c.Param("agent_id"), tags)
	if err != nil {
		jsonResponse(c, code, false, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, updated, "success")
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

// ListAgentVersions returns an array of summaries after authorizing the path agent.
func (h *AgentHandler) ListAgentVersions(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	rows, code, err := h.agentService.ListVersions(c.Request.Context(), user.ID, c.Param("agent_id"))
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, rows, "")
}

// GetAgentVersion returns DSL only for a version belonging to the authorized agent.
func (h *AgentHandler) GetAgentVersion(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	row, code, err := h.agentService.GetVersion(c.Request.Context(), user.ID, c.Param("agent_id"), c.Param("version_id"))
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, row, "")
}

// UploadAgentFile accepts repeated multipart file fields for authenticated callers.
// Public beta uploads continue through the Python gateway and never gain a Go principal.
func (h *AgentHandler) UploadAgentFile(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	// Authorize before parsing a body that may spill to temporary files.
	if _, code, err := h.agentService.AuthorizeUpload(c.Request.Context(), user.ID, c.Param("agent_id")); err != nil {
		jsonError(c, code, err.Error())
		return
	}
	// URL.Query silently drops malformed escapes. This endpoint accepts no
	// query component, including an empty '?' marker, before parsing files.
	if c.Request.URL.RawQuery != "" || c.Request.URL.ForceQuery {
		jsonError(c, common.CodeArgumentError, "Provide multipart file fields only.")
		return
	}
	err := c.Request.ParseMultipartForm(32 << 20)
	if c.Request.MultipartForm != nil {
		defer c.Request.MultipartForm.RemoveAll()
	}
	if err != nil {
		jsonError(c, common.CodeArgumentError, "Invalid multipart form.")
		return
	}
	form := c.Request.MultipartForm
	if len(form.Value) != 0 || len(form.File) != 1 || len(form.File["file"]) == 0 {
		jsonError(c, common.CodeArgumentError, "Provide multipart file fields only.")
		return
	}
	result, code, err := h.agentService.UploadFiles(c.Request.Context(), user.ID, c.Param("agent_id"), form.File["file"])
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, result, "")
}

// parseAgentSessionQuery rejects malformed escapes and repeated controls before DB access.
func parseAgentSessionQuery(raw string) (service.ListAgentSessionsRequest, error) {
	req := service.ListAgentSessionsRequest{Page: 1, PageSize: 30, OrderBy: "update_time", Desc: true, IncludeDSL: true}
	query, err := url.ParseQuery(raw)
	if err != nil {
		return req, fmt.Errorf("invalid query parameter")
	}
	for _, key := range []string{"id", "user_id", "page", "page_size", "keywords", "from_date", "to_date", "orderby", "exp_user_id", "desc", "dsl"} {
		if values, ok := query[key]; ok && (len(values) != 1 || ((key == "page" || key == "page_size" || key == "orderby" || key == "desc" || key == "dsl" || key == "from_date" || key == "to_date") && strings.TrimSpace(values[0]) == "")) {
			return req, fmt.Errorf("invalid query parameter")
		}
	}
	req.SessionID = query.Get("id")
	req.UserID = query.Get("user_id")
	req.Keywords = query.Get("keywords")
	req.FromDate = query.Get("from_date")
	req.ToDate = query.Get("to_date")
	req.ExpUserID = query.Get("exp_user_id")
	for key, dest := range map[string]*int{"page": &req.Page, "page_size": &req.PageSize} {
		if values, ok := query[key]; ok {
			value, e := strconv.Atoi(values[0])
			if e != nil {
				return req, fmt.Errorf("invalid pagination")
			}
			*dest = value
		}
	}
	if value, ok := query["orderby"]; ok {
		req.OrderBy = value[0]
	}
	for key, dest := range map[string]*bool{"desc": &req.Desc, "dsl": &req.IncludeDSL} {
		if values, ok := query[key]; ok {
			switch values[0] {
			case "true", "True":
				*dest = true
			case "false", "False":
				*dest = false
			default:
				return req, fmt.Errorf("invalid %s", key)
			}
		}
	}
	return req, nil
}

func (h *AgentHandler) ListAgentSessions(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	req, err := parseAgentSessionQuery(c.Request.URL.RawQuery)
	if err != nil {
		jsonError(c, common.CodeArgumentError, err.Error())
		return
	}
	result, code, err := h.agentService.ListAgentSessions(c.Request.Context(), user.ID, c.Param("agent_id"), req)
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, result, "success")
}

func (h *AgentHandler) GetAgentSession(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	result, code, err := h.agentService.GetAgentSession(c.Request.Context(), user.ID, c.Param("agent_id"), c.Param("session_id"))
	if err != nil {
		jsonError(c, code, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, result, "success")
}

func (h *AgentHandler) DeleteAgentSessionItem(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonResponse(c, code, false, message)
		return
	}
	deleted, code, err := h.agentService.DeleteAgentSessionItem(c.Request.Context(), user.ID, c.Param("agent_id"), c.Param("session_id"))
	if err != nil {
		jsonResponse(c, code, false, err.Error())
		return
	}
	jsonResponse(c, common.CodeSuccess, deleted, "success")
}

// GetPrompts returns the default agent prompts after authenticating the actor.
func (h *AgentHandler) GetPrompts(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	if user == nil || user.ID == "" {
		jsonError(c, common.CodeUnauthorized, "User not found")
		return
	}
	load := h.loadPrompt
	if load == nil {
		load = service.LoadPrompt
	}
	names := []string{"analyze_task_system", "analyze_task_user", "next_step", "reflect", "citation_prompt"}
	prompts := make([]string, len(names))
	for i, name := range names {
		value, err := load(name)
		if err != nil {
			jsonError(c, common.CodeServerError, "Agent prompts are unavailable.")
			return
		}
		prompts[i] = value
	}
	jsonResponse(c, common.CodeSuccess, map[string]string{
		"task_analysis":   prompts[0] + "\n\n" + prompts[1],
		"plan_generation": prompts[2], "reflection": prompts[3], "citation_guidelines": prompts[4],
	}, "success")
}
