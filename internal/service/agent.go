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
	"math"
	"sort"
	"strconv"
	"strings"
	"time"
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

// ListAgentSessionsRequest carries filters separately from the authenticated actor.
type ListAgentSessionsRequest struct {
	SessionID, UserID, Keywords, FromDate, ToDate, OrderBy, ExpUserID string
	Page, PageSize                                                    int
	Desc, IncludeDSL                                                  bool
}

type ListAgentSessionsResponse struct {
	Sessions []map[string]any `json:"sessions"`
	Total    int64            `json:"total"`
}

func parseAgentSessionDate(value string, isEnd bool) (*time.Time, error) {
	if value == "" {
		return nil, nil
	}
	var parsed time.Time
	var err error
	switch {
	case len(value) == 10:
		parsed, err = time.ParseInLocation("2006-01-02", value, time.Local)
		if err == nil && isEnd {
			parsed = parsed.AddDate(0, 0, 1).Add(-time.Nanosecond)
		}
	case strings.Contains(value, "T"):
		parsed, err = time.Parse(time.RFC3339Nano, value)
	default:
		parsed, err = time.ParseInLocation("2006-01-02 15:04:05", value, time.Local)
	}
	if err != nil {
		return nil, fmt.Errorf("invalid session date")
	}
	// Python query dates use naive local wall times; pgx needs local components
	// when binding a timestamp-without-time-zone column.
	parsed = parsed.Local()
	return &parsed, nil
}

func decodeAgentSessionJSON(raw json.RawMessage) (any, error) {
	if len(raw) == 0 {
		return nil, nil
	}
	var value any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	return value, nil
}

// firstAgentChunkValue respects field presence, including explicit null, 0 and empty text.
func firstAgentChunkValue(chunk map[string]any, primary, fallback string) any {
	if value, ok := chunk[primary]; ok {
		return value
	}
	return chunk[fallback]
}

func normalizeAgentReferenceChunk(raw any) map[string]any {
	chunk, ok := raw.(map[string]any)
	if !ok {
		var content string
		switch value := raw.(type) {
		case string:
			if strings.TrimSpace(value) == "" {
				return nil
			}
			content = value
		case json.Number:
			// Legacy chunk IDs are strings or integers; floats and booleans are not IDs.
			integer := strings.TrimPrefix(value.String(), "-")
			if integer == "" || strings.IndexFunc(integer, func(r rune) bool { return r < '0' || r > '9' }) >= 0 {
				return nil
			}
			content = value.String()
		default:
			return nil
		}
		chunk = map[string]any{"id": raw, "content": content}
	}
	result := map[string]any{
		"id":            firstAgentChunkValue(chunk, "chunk_id", "id"),
		"content":       firstAgentChunkValue(chunk, "content_with_weight", "content"),
		"document_id":   firstAgentChunkValue(chunk, "doc_id", "document_id"),
		"document_name": firstAgentChunkValue(chunk, "docnm_kwd", "document_name"),
		"dataset_id":    firstAgentChunkValue(chunk, "kb_id", "dataset_id"),
		"image_id":      firstAgentChunkValue(chunk, "image_id", "img_id"),
		"positions":     firstAgentChunkValue(chunk, "positions", "position_int"),
	}
	for _, key := range []string{"doc_type", "doc_type_kwd"} {
		if value, ok := chunk[key]; ok {
			result[key] = value
		}
	}
	return result
}

func normalizeAgentReferenceChunks(raw any) []map[string]any {
	result := []map[string]any{}
	switch chunks := raw.(type) {
	case []any:
		indices := []int{}
		shifted := false
		for index, raw := range chunks {
			if chunk := normalizeAgentReferenceChunk(raw); chunk != nil {
				shifted = shifted || index != len(result)
				indices = append(indices, index)
				result = append(result, chunk)
			}
		}
		// Filtering malformed chunks must not renumber stored [ID:n] citations.
		// Keep contiguous legacy output unchanged; sparse lists carry every source index.
		if shifted {
			for index, chunk := range result {
				chunk["citation_id"] = strconv.Itoa(indices[index])
			}
		}
	case map[string]any:
		keys := make([]string, 0, len(chunks))
		for key := range chunks {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		for _, key := range keys {
			if chunk := normalizeAgentReferenceChunk(chunks[key]); chunk != nil {
				chunk["citation_id"] = key
				result = append(result, chunk)
			}
		}
	}
	return result
}

func agentSessionReferences(raw any) ([]any, error) {
	switch refs := raw.(type) {
	case []any:
		return refs, nil
	case map[string]any:
		if _, ok := refs["chunks"]; ok {
			return []any{refs}, nil
		}
		keys := make([]string, 0, len(refs))
		positions := map[string]int{}
		for key := range refs {
			position, err := strconv.Atoi(key)
			if err != nil {
				return nil, fmt.Errorf("invalid session references")
			}
			positions[key] = position
			keys = append(keys, key)
		}
		sort.Slice(keys, func(i, j int) bool {
			if positions[keys[i]] != positions[keys[j]] {
				return positions[keys[i]] < positions[keys[j]]
			}
			return keys[i] < keys[j]
		})
		values := make([]any, 0, len(keys))
		for _, key := range keys {
			values = append(values, refs[key])
		}
		return values, nil
	}
	return nil, nil
}

func normalizeAgentSession(session *dao.AgentSession, includeDSL bool) (map[string]any, error) {
	raw, err := json.Marshal(session)
	if err != nil {
		return nil, err
	}
	decoded, err := decodeAgentSessionJSON(raw)
	if err != nil {
		return nil, err
	}
	result := decoded.(map[string]any)
	rawMessages := result["message"]
	messages := []map[string]any{}
	if rawMessages != nil {
		list, ok := rawMessages.([]any)
		if !ok {
			return nil, fmt.Errorf("invalid session messages")
		}
		for _, value := range list {
			message, ok := value.(map[string]any)
			if !ok {
				return nil, fmt.Errorf("invalid session message")
			}
			delete(message, "prompt")
			messages = append(messages, message)
		}
	}
	references := result["reference"]
	refs, err := agentSessionReferences(references)
	if err != nil {
		return nil, err
	}
	referenceIndex := 0
	for i, message := range messages {
		if i == 0 || message["role"] == "user" {
			continue
		}
		if referenceIndex >= len(refs) {
			break
		}
		ref, _ := refs[referenceIndex].(map[string]any)
		referenceIndex++
		chunks, ok := ref["chunks"]
		if !ok {
			chunks = ref["reference"]
		}
		message["reference"] = normalizeAgentReferenceChunks(chunks)
	}
	result["messages"] = messages
	result["agent_id"] = session.DialogID
	delete(result, "message")
	delete(result, "reference")
	delete(result, "dialog_id")
	if !includeDSL {
		delete(result, "dsl")
	}
	return result, nil
}

func (s *AgentService) ListAgentSessions(ctx context.Context, actorID, agentID string, req ListAgentSessionsRequest) (*ListAgentSessionsResponse, common.ErrorCode, error) {
	if err := dao.ValidateAgentSessionPagination(req.Page, req.PageSize); err != nil {
		return nil, common.CodeArgumentError, err
	}
	if !dao.ValidAgentSessionOrderBy(req.OrderBy) {
		return nil, common.CodeArgumentError, fmt.Errorf("invalid orderby")
	}
	from, err := parseAgentSessionDate(req.FromDate, false)
	if err != nil {
		return nil, common.CodeArgumentError, err
	}
	to, err := parseAgentSessionDate(req.ToDate, true)
	if err != nil {
		return nil, common.CodeArgumentError, err
	}
	if from != nil && to != nil && from.After(*to) {
		return nil, common.CodeArgumentError, fmt.Errorf("invalid date range")
	}
	if _, code, err := s.requireCanvasAccess(ctx, actorID, agentID); err != nil {
		return nil, code, err
	}
	sessionDAO := dao.NewChatSessionDAO()
	if req.ExpUserID != "" {
		rows, err := sessionDAO.ListAgentSessionNames(ctx, agentID, req.ExpUserID)
		if err != nil {
			return nil, common.CodeServerError, fmt.Errorf("Unable to list agent sessions.")
		}
		items := make([]map[string]any, 0, len(rows))
		for _, row := range rows {
			items = append(items, map[string]any{"id": row.ID, "name": row.Name})
		}
		return &ListAgentSessionsResponse{Sessions: items, Total: int64(len(items))}, common.CodeSuccess, nil
	}
	total, rows, err := sessionDAO.ListAgentSessions(ctx, dao.ListAgentSessionsParams{AgentID: agentID, Page: req.Page, PageSize: req.PageSize, OrderBy: req.OrderBy, Desc: req.Desc, SessionID: req.SessionID, UserID: req.UserID, Keywords: req.Keywords, IncludeDSL: req.IncludeDSL, FromDate: from, ToDate: to})
	if err != nil {
		return nil, common.CodeServerError, fmt.Errorf("Unable to list agent sessions.")
	}
	items := make([]map[string]any, 0, len(rows))
	for _, row := range rows {
		item, err := normalizeAgentSession(row, req.IncludeDSL)
		if err != nil {
			return nil, common.CodeServerError, fmt.Errorf("Unable to read agent session.")
		}
		items = append(items, item)
	}
	return &ListAgentSessionsResponse{Sessions: items, Total: total}, common.CodeSuccess, nil
}

func (s *AgentService) GetAgentSession(ctx context.Context, actorID, agentID, sessionID string) (map[string]any, common.ErrorCode, error) {
	if sessionID == "" {
		return nil, common.CodeArgumentError, fmt.Errorf("session_id is required")
	}
	if _, code, err := s.requireCanvasAccess(ctx, actorID, agentID); err != nil {
		return nil, code, err
	}
	row, err := dao.NewAPI4ConversationDAO().GetBySessionID(ctx, sessionID, agentID)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, common.CodeNotFound, fmt.Errorf("Agent session not found.")
	}
	if err != nil {
		return nil, common.CodeServerError, fmt.Errorf("Unable to read agent session.")
	}
	result, err := normalizeAgentSession(row, true)
	if err != nil {
		return nil, common.CodeServerError, fmt.Errorf("Unable to read agent session.")
	}
	return result, common.CodeSuccess, nil
}

func (s *AgentService) DeleteAgentSessionItem(ctx context.Context, actorID, agentID, sessionID string) (bool, common.ErrorCode, error) {
	if sessionID == "" {
		return false, common.CodeArgumentError, fmt.Errorf("session_id is required")
	}
	if _, code, err := s.requireCanvasAccess(ctx, actorID, agentID); err != nil {
		return false, code, err
	}
	rows, err := dao.NewAPI4ConversationDAO().DeleteBySessionIDAndAgentID(ctx, sessionID, agentID)
	if err != nil {
		return false, common.CodeServerError, fmt.Errorf("Unable to delete agent session.")
	}
	return rows > 0, common.CodeSuccess, nil
}
