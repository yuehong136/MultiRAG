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
	"math"
	"sort"
	"strings"
	"unicode"

	"golang.org/x/text/cases"
	"golang.org/x/text/language"
	"gorm.io/gorm"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
)

// AgentService agent service
type AgentService struct {
	canvasDAO     *dao.UserCanvasDAO
	userTenantDAO *dao.UserTenantDAO
}

// NewAgentService create agent service
func NewAgentService() *AgentService {
	return &AgentService{
		canvasDAO:     dao.NewUserCanvasDAO(),
		userTenantDAO: dao.NewUserTenantDAO(),
	}
}

// ListTemplates returns the full system catalogue for an authenticated caller.
func (s *AgentService) ListTemplates(ctx context.Context, userID string) ([]*entity.CanvasTemplate, common.ErrorCode, error) {
	if userID == "" {
		return nil, common.CodeAuthenticationError, fmt.Errorf("No authorization.")
	}
	rows, err := dao.NewCanvasTemplateDAO().GetAll(ctx)
	if err != nil {
		return nil, common.CodeServerError, fmt.Errorf("Unable to list agent templates.")
	}
	return rows, common.CodeSuccess, nil
}

// normalizeAgentTags preserves first spelling and order within Unicode limits.
// The accepted types match the production Python string/list-of-strings contract.
func normalizeAgentTags(raw any) (string, error) {
	var tags []string
	switch value := raw.(type) {
	case string:
		tags = strings.Split(value, ",")
	case []string:
		tags = make([]string, len(value))
		for i, tag := range value {
			tags[i] = strings.ReplaceAll(tag, ",", " ")
		}
	default:
		return "", fmt.Errorf("tags must be a string or array of strings")
	}
	result := []string{}
	seen := map[string]bool{}
	used := 0
	lower := cases.Lower(language.Und)
	for _, tag := range tags {
		runes := []rune(strings.TrimFunc(tag, func(r rune) bool { return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f) }))
		if len(runes) > 64 {
			runes = runes[:64]
		}
		tag = string(runes)
		key := lower.String(tag)
		if tag == "" || seen[key] {
			continue
		}
		extra := len(runes)
		if len(result) > 0 {
			extra++
		}
		if used+extra > 512 {
			break
		}
		seen[key] = true
		result = append(result, tag)
		used += extra
	}
	return strings.Join(result, ","), nil
}

// UpdateAgentTags manages only the caller's canvas, regardless of team read access.
func (s *AgentService) UpdateAgentTags(ctx context.Context, userID, canvasID string, tags any) (bool, common.ErrorCode, error) {
	if userID == "" {
		return false, common.CodeAuthenticationError, fmt.Errorf("No authorization.")
	}
	if canvasID == "" {
		return false, common.CodeArgumentError, fmt.Errorf("agent_id is required")
	}
	normalized, err := normalizeAgentTags(tags)
	if err != nil {
		return false, common.CodeArgumentError, err
	}
	err = s.canvasDAO.UpdateTagsOwned(ctx, userID, canvasID, normalized)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return false, common.CodeOperatingError, fmt.Errorf("Agent not found or no permission.")
	}
	if err != nil {
		return false, common.CodeServerError, fmt.Errorf("Unable to update agent tags.")
	}
	return true, common.CodeSuccess, nil
}

// AgentItem is one entry in the list response.
type AgentItem struct {
	ID             string  `json:"id"`
	Avatar         *string `json:"avatar,omitempty"`
	Title          *string `json:"title,omitempty"`
	Permission     string  `json:"permission"`
	CanvasType     *string `json:"canvas_type,omitempty"`
	CanvasCategory string  `json:"canvas_category"`
	CreateTime     *int64  `json:"create_time,omitempty"`
	UpdateTime     *int64  `json:"update_time,omitempty"`
}

// ListAgentsResponse is the response body for GET /api/v1/agents.
type ListAgentsResponse struct {
	Canvas []*AgentItem `json:"canvas"`
	Total  int64        `json:"total"`
}

func toAgentItem(c *entity.UserCanvas) *AgentItem {
	return &AgentItem{
		ID:             c.ID,
		Avatar:         c.Avatar,
		Title:          c.Title,
		Permission:     c.Permission,
		CanvasType:     c.CanvasType,
		CanvasCategory: c.CanvasCategory,
		CreateTime:     c.CreateTime,
		UpdateTime:     c.UpdateTime,
	}
}

// ListAgents returns agent canvases visible to userID.
// Mirrors Python agent_api.list_agents — validates owner_ids against joined tenants,
// then delegates to the DAO.
func (s *AgentService) ListAgents(
	ctx context.Context,
	userID string,
	keywords string,
	page, pageSize int,
	orderby string,
	desc bool,
	ownerIDs []string,
	canvasCategory string,
) (*ListAgentsResponse, common.ErrorCode, error) {
	if userID == "" {
		return nil, common.CodeAuthenticationError, fmt.Errorf("No authorization.")
	}
	if page < 0 || pageSize < 0 || pageSize > 1000 || (page == 0) != (pageSize == 0) || (pageSize > 0 && page > math.MaxInt/pageSize) {
		return nil, common.CodeArgumentError, fmt.Errorf("invalid pagination")
	}
	switch orderby {
	case "create_time", "update_time", "title", "id":
	default:
		return nil, common.CodeArgumentError, fmt.Errorf("invalid orderby")
	}
	switch canvasCategory {
	case "":
		canvasCategory = "agent_canvas"
	case "agent_canvas", "dataflow_canvas":
	default:
		return nil, common.CodeArgumentError, fmt.Errorf("invalid canvas_category")
	}
	// Build the set of tenant IDs the user is authorised to query.
	tenantIDs, err := s.userTenantDAO.GetTenantIDsByUserID(userID)
	if err != nil {
		return nil, common.CodeServerError, fmt.Errorf("Unable to read tenant memberships.")
	}
	authorised := make(map[string]struct{}, len(tenantIDs)+1)
	for _, id := range tenantIDs {
		authorised[id] = struct{}{}
	}
	authorised[userID] = struct{}{}

	var effectiveOwnerIDs []string
	if ownerIDs != nil {
		for _, id := range ownerIDs {
			if _, ok := authorised[id]; !ok {
				return nil, common.CodeOperatingError, fmt.Errorf("only authorized owner_ids can be queried")
			}
		}
		effectiveOwnerIDs = append([]string{}, ownerIDs...)
	} else {
		effectiveOwnerIDs = make([]string, 0, len(authorised))
		for id := range authorised {
			effectiveOwnerIDs = append(effectiveOwnerIDs, id)
		}
	}

	sort.Strings(effectiveOwnerIDs)
	canvases, total, err := s.canvasDAO.ListByTenantIDs(
		ctx,
		effectiveOwnerIDs,
		userID,
		page,
		pageSize,
		orderby,
		desc,
		keywords,
		canvasCategory,
	)
	if err != nil {
		return nil, common.CodeServerError, fmt.Errorf("Unable to list agents.")
	}

	items := make([]*AgentItem, len(canvases))
	for i, c := range canvases {
		items[i] = toAgentItem(c)
	}
	return &ListAgentsResponse{Canvas: items, Total: total}, common.CodeSuccess, nil
}

// requireCanvasAccess binds the path canvas to an active owner and accepted TEAM membership.
// Loading authorization with the request context keeps cancellation and DB failures distinct.
func (s *AgentService) requireCanvasAccess(ctx context.Context, userID, canvasID string) (*entity.UserCanvas, common.ErrorCode, error) {
	if userID == "" {
		return nil, common.CodeAuthenticationError, fmt.Errorf("No authorization.")
	}
	if canvasID == "" {
		return nil, common.CodeArgumentError, fmt.Errorf("agent_id is required")
	}
	canvas, err := dao.NewUserCanvasVersionDAO().AccessibleCanvas(ctx, userID, canvasID)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, common.CodeOperatingError, fmt.Errorf("Agent not found or no permission.")
	}
	if err != nil {
		return nil, common.CodeServerError, fmt.Errorf("Unable to read agent access.")
	}
	return canvas, common.CodeSuccess, nil
}

// ListVersions returns summaries only, including the published flag used by consumers.
func (s *AgentService) ListVersions(ctx context.Context, userID, canvasID string) ([]*dao.AgentVersionSummary, common.ErrorCode, error) {
	if _, code, err := s.requireCanvasAccess(ctx, userID, canvasID); err != nil {
		return nil, code, err
	}
	versions, err := dao.NewUserCanvasVersionDAO().ListByCanvasID(ctx, canvasID)
	if err != nil {
		return nil, common.CodeServerError, fmt.Errorf("Unable to list agent versions.")
	}
	return versions, common.CodeSuccess, nil
}

// GetVersion scopes the SQL lookup by both version and its authorized path canvas.
func (s *AgentService) GetVersion(ctx context.Context, userID, canvasID, versionID string) (*entity.UserCanvasVersion, common.ErrorCode, error) {
	if versionID == "" {
		return nil, common.CodeArgumentError, fmt.Errorf("version_id is required")
	}
	if _, code, err := s.requireCanvasAccess(ctx, userID, canvasID); err != nil {
		return nil, code, err
	}
	version, err := dao.NewUserCanvasVersionDAO().GetByCanvasID(ctx, canvasID, versionID)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, common.CodeNotFound, fmt.Errorf("Version not found.")
	}
	if err != nil {
		return nil, common.CodeServerError, fmt.Errorf("Unable to read agent version.")
	}
	return version, common.CodeSuccess, nil
}

// AuthorizeUpload allows handlers to reject access before parsing multipart data.
func (s *AgentService) AuthorizeUpload(ctx context.Context, userID, canvasID string) (*entity.UserCanvas, common.ErrorCode, error) {
	return s.requireCanvasAccess(ctx, userID, canvasID)
}
