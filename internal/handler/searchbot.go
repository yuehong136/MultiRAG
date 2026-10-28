package handler

import (
	"context"
	"errors"
	"strings"

	"github.com/gin-gonic/gin"

	"multirag/internal/common"
	"multirag/internal/service"
)

type relatedQuestionsService interface {
	RelatedQuestions(context.Context, string, string, string) ([]string, error)
}

// SearchbotRequest contains the caller's question and optional saved search.
type SearchbotRequest struct {
	Question string `json:"question"`
	SearchID string `json:"search_id,omitempty"`
}

// SearchbotHandler generates related questions for authenticated search users.
type SearchbotHandler struct {
	service relatedQuestionsService
}

func NewSearchbotHandler(svc relatedQuestionsService) *SearchbotHandler {
	return &SearchbotHandler{service: svc}
}

func (h *SearchbotHandler) Handle(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	if user == nil || strings.TrimSpace(user.ID) == "" {
		jsonError(c, common.CodeUnauthorized, "User not found")
		return
	}
	var req SearchbotRequest
	if err := bindObjectJSON(c, &req); err != nil || strings.TrimSpace(req.Question) == "" || req.SearchID != strings.TrimSpace(req.SearchID) {
		jsonError(c, common.CodeArgumentError, "Invalid related questions request")
		return
	}
	if h.service == nil {
		jsonError(c, common.CodeOperatingError, "Related questions are unavailable")
		return
	}
	questions, err := h.service.RelatedQuestions(c.Request.Context(), user.ID, req.Question, req.SearchID)
	if err != nil {
		switch {
		case errors.Is(err, service.ErrSearchbotArgument):
			jsonError(c, common.CodeArgumentError, "Invalid related questions request")
		case errors.Is(err, service.ErrSearchbotAccessDenied):
			jsonError(c, common.CodeAuthenticationError, "No authorization for the selected search")
		default:
			jsonError(c, common.CodeOperatingError, "Related questions are unavailable")
		}
		return
	}
	if questions == nil {
		questions = []string{}
	}
	jsonResponse(c, common.CodeSuccess, questions, "")
}
