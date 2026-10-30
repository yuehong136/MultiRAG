package service

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"gorm.io/gorm"
	"multirag/internal/common"
	"multirag/internal/dao"
)

type DeleteAgentSessionsRequest struct {
	// nil means omitted; an explicit [] must never expand to all sessions.
	IDs       []string `json:"ids"`
	DeleteAll bool     `json:"delete_all"`
}

type DeleteAgentSessionsResponse struct {
	SuccessCount int      `json:"success_count"`
	Errors       []string `json:"errors,omitempty"`
}

type DeleteAgentSessionsResult struct {
	Data    *DeleteAgentSessionsResponse `json:"data,omitempty"`
	Message string                       `json:"message,omitempty"`
}

// DeleteAgentSessions requires ownership even for a no-op; TEAM read access is insufficient.
func (s *AgentService) DeleteAgentSessions(ctx context.Context, actorID, agentID string, req DeleteAgentSessionsRequest) (*DeleteAgentSessionsResult, common.ErrorCode, error) {
	if actorID == "" {
		return nil, common.CodeAuthenticationError, errors.New("No authorization.")
	}
	if strings.TrimSpace(agentID) == "" || len(req.IDs) > 1000 {
		return nil, common.CodeArgumentError, errors.New("Invalid agent or session IDs.")
	}
	ids := req.IDs
	var duplicates []string
	if ids != nil {
		ids = make([]string, 0, len(req.IDs))
		seen, repeated := map[string]bool{}, map[string]bool{}
		for _, raw := range req.IDs {
			id := strings.TrimSpace(raw)
			if id == "" || len(id) > 255 {
				return nil, common.CodeArgumentError, errors.New("Invalid session ID.")
			}
			if seen[id] {
				if !repeated[id] {
					duplicates = append(duplicates, "Duplicate session ids: "+id)
					repeated[id] = true
				}
				continue
			}
			seen[id] = true
			ids = append(ids, id)
		}
	}
	count, missing, err := dao.NewAPI4ConversationDAO().DeleteAgentSessionsOwned(ctx, actorID, agentID, ids, req.DeleteAll)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, common.CodeOperatingError, errors.New("Agent not found or no permission.")
	}
	if err != nil {
		return nil, common.CodeServerError, errors.New("Unable to delete agent sessions.")
	}
	problems := append(duplicates, missing...)
	if len(problems) == 0 {
		return &DeleteAgentSessionsResult{}, common.CodeSuccess, nil
	}
	if count == 0 {
		return nil, common.CodeDataError, errors.New(strings.Join(problems, "; "))
	}
	return &DeleteAgentSessionsResult{Data: &DeleteAgentSessionsResponse{SuccessCount: count, Errors: problems}, Message: fmt.Sprintf("Partially deleted %d sessions with %d errors", count, len(problems))}, common.CodeSuccess, nil
}
