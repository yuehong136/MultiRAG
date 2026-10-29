package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
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

type chunkRetriever interface {
	RetrievalTest(context.Context, *service.RetrievalTestRequest, string) (*service.RetrievalTestResponse, error)
}

// SearchbotRetrievalTestRequest keeps kb_id separate from the array-only
// datasets/search contract. Numeric model IDs and metadata numbers stay exact.
type SearchbotRetrievalTestRequest struct {
	KBIDs                  common.StringSlice     `json:"kb_id"`
	Question               string                 `json:"question"`
	Page                   *int                   `json:"page,omitempty"`
	Size                   *int                   `json:"size,omitempty"`
	DocIDs                 []string               `json:"doc_ids,omitempty"`
	UseKG                  *bool                  `json:"use_kg,omitempty"`
	TopK                   *int                   `json:"top_k,omitempty"`
	CrossLanguages         []string               `json:"cross_languages,omitempty"`
	SearchID               *string                `json:"search_id,omitempty"`
	Filter                 map[string]interface{} `json:"meta_data_filter,omitempty"`
	TenantRerankID         *int64                 `json:"tenant_rerank_id,omitempty"`
	RerankID               *string                `json:"rerank_id,omitempty"`
	Keyword                *bool                  `json:"keyword,omitempty"`
	SimilarityThreshold    *float64               `json:"similarity_threshold,omitempty"`
	VectorSimilarityWeight *float64               `json:"vector_similarity_weight,omitempty"`
	Highlight              *bool                  `json:"highlight,omitempty"`
}

func (r *SearchbotRetrievalTestRequest) UnmarshalJSON(raw []byte) error {
	type requestJSON SearchbotRetrievalTestRequest
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	decoder.DisallowUnknownFields()
	return decoder.Decode((*requestJSON)(r))
}

// SearchbotHandler serves retrieval and related questions for authenticated users.
type SearchbotHandler struct {
	service   relatedQuestionsService
	retriever chunkRetriever
}

func NewSearchbotHandler(svc relatedQuestionsService, retrievers ...chunkRetriever) *SearchbotHandler {
	h := &SearchbotHandler{service: svc}
	if len(retrievers) > 0 {
		h.retriever = retrievers[0]
	}
	return h
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

// RetrievalTest handles POST /api/v1/searchbots/retrieval_test through the same
// authorized service as datasets/search. Authentication is supplied by Router.
func (h *SearchbotHandler) RetrievalTest(c *gin.Context) {
	user, code, message := GetUser(c)
	if code != common.CodeSuccess {
		jsonError(c, code, message)
		return
	}
	if user == nil || strings.TrimSpace(user.ID) == "" {
		jsonError(c, common.CodeUnauthorized, "User not found")
		return
	}
	var req SearchbotRetrievalTestRequest
	if err := bindObjectJSONNullable(c, &req, map[string]bool{"doc_ids": true, "cross_languages": true, "search_id": true, "meta_data_filter": true, "tenant_rerank_id": true, "rerank_id": true}); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"code": common.CodeBadRequest, "data": nil, "message": "Invalid retrieval request"})
		return
	}
	invalid := len(req.KBIDs) == 0
	for _, id := range req.KBIDs {
		if strings.TrimSpace(id) == "" || id != strings.TrimSpace(id) {
			invalid = true
		}
	}
	if req.SearchID != nil && *req.SearchID != strings.TrimSpace(*req.SearchID) {
		invalid = true
	}
	for _, id := range req.DocIDs {
		if strings.TrimSpace(id) == "" || id != strings.TrimSpace(id) {
			invalid = true
		}
	}
	mapped := &service.RetrievalTestRequest{
		DatasetIDs: []string(req.KBIDs), Question: req.Question, Page: req.Page, Size: req.Size,
		DocIDs: req.DocIDs, UseKG: req.UseKG, TopK: req.TopK, CrossLanguages: req.CrossLanguages,
		SearchID: req.SearchID, Filter: req.Filter, TenantRerankID: req.TenantRerankID,
		RerankID: req.RerankID, Keyword: req.Keyword, SimilarityThreshold: req.SimilarityThreshold,
		VectorSimilarityWeight: req.VectorSimilarityWeight, Highlight: req.Highlight,
	}
	if invalid || service.ValidateDatasetSearchRequest(mapped) != nil {
		c.JSON(http.StatusBadRequest, gin.H{"code": common.CodeBadRequest, "data": nil, "message": "Invalid retrieval request"})
		return
	}
	// Defaults apply only when absent; saved settings may subsequently override
	// them, including explicit zero/false/[] controls.
	if mapped.Page == nil {
		value := 1
		mapped.Page = &value
	}
	if mapped.Size == nil {
		value := 30
		mapped.Size = &value
	}
	if mapped.TopK == nil {
		value := 1024
		mapped.TopK = &value
	}
	if mapped.UseKG == nil {
		value := false
		mapped.UseKG = &value
	}
	if mapped.Keyword == nil {
		value := false
		mapped.Keyword = &value
	}
	if mapped.SimilarityThreshold == nil {
		value := 0.0
		mapped.SimilarityThreshold = &value
	}
	if mapped.VectorSimilarityWeight == nil {
		value := 0.3
		mapped.VectorSimilarityWeight = &value
	}
	if h.retriever == nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"code": common.CodeOperatingError, "data": nil, "message": "Retrieval is unavailable"})
		return
	}
	result, err := h.retriever.RetrievalTest(c.Request.Context(), mapped, user.ID)
	switch {
	case errors.Is(err, service.ErrRetrievalArgument), errors.Is(err, service.ErrSearchbotArgument):
		c.JSON(http.StatusBadRequest, gin.H{"code": common.CodeBadRequest, "data": nil, "message": "Invalid retrieval request"})
	case errors.Is(err, service.ErrDatasetAccessDenied), errors.Is(err, service.ErrSearchbotAccessDenied):
		jsonError(c, common.CodeForbidden, "No authorization for the selected resources")
	case err != nil || result == nil:
		c.JSON(http.StatusInternalServerError, gin.H{"code": common.CodeServerError, "data": nil, "message": "Retrieval failed"})
	default:
		jsonResponse(c, common.CodeSuccess, result, "success")
	}
}
