package handler

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"sort"
	"strings"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/service/kg"
	"multirag/internal/service/nlp"
)

// DifyDatasetReader resolves active membership and dataset visibility together.
type DifyDatasetReader interface {
	GetReadableByID(context.Context, string, string) (*entity.Knowledgebase, error)
	GetByIDContext(context.Context, string) (*entity.Knowledgebase, error)
}

// DifyModelBinder binds owner-scoped numeric model references before use.
type DifyModelBinder interface {
	Embedding(context.Context, *entity.Knowledgebase) (*models.EmbeddingModel, error)
	Chat(context.Context, *entity.Knowledgebase) (*models.ChatModel, error)
}

// DifyMetadataReader supplies typed filtering and exact document enrichment.
type DifyMetadataReader interface {
	GetFlattedMetaByKBsContext(context.Context, []string) (common.MetaData, error)
	EnrichChunksWithDocMetadata(context.Context, []map[string]interface{}, string, []string) ([]map[string]interface{}, error)
	LabelQuestionContext(context.Context, string, string, []*entity.Knowledgebase) (map[string]float64, error)
}

type DifyChunkRetriever interface {
	Retrieval(context.Context, *nlp.RetrievalRequest) (*nlp.RetrievalResult, error)
}

type DifyDocumentReader interface {
	GetByIDs(context.Context, []string) ([]*entity.Document, error)
}

type DifyKGRetriever interface {
	Retrieval(context.Context, string, []string, string, *models.ChatModel, *models.EmbeddingModel, ...kg.Option) (*kg.Result, error)
}

// DifyChildrenRetriever propagates parent lookup failures and cancellation.
type DifyChildrenRetriever func(context.Context, []map[string]interface{}, string) ([]map[string]interface{}, error)

// DifyRetrievalDependencies keeps storage and model boundaries explicit.
type DifyRetrievalDependencies struct {
	Datasets  DifyDatasetReader
	Models    DifyModelBinder
	Metadata  DifyMetadataReader
	Retrieval DifyChunkRetriever
	Documents DifyDocumentReader
	Children  DifyChildrenRetriever
	KG        DifyKGRetriever
}

type DifyRetrievalHandler struct{ deps DifyRetrievalDependencies }

func NewDifyRetrievalHandler(deps DifyRetrievalDependencies) *DifyRetrievalHandler {
	return &DifyRetrievalHandler{deps: deps}
}

type difyRetrievalRequest struct {
	KnowledgeID       string                 `json:"knowledge_id"`
	Query             string                 `json:"query"`
	UseKG             bool                   `json:"use_kg"`
	RetrievalSetting  *difyRetrievalSetting  `json:"retrieval_setting"`
	MetadataCondition *difyMetadataCondition `json:"metadata_condition"`
	SearchMode        json.RawMessage        `json:"search_mode"`
}

type difyRetrievalSetting struct {
	TopK           *int     `json:"top_k"`
	ScoreThreshold *float64 `json:"score_threshold"`
}

type difyCondition struct {
	Name               string      `json:"name"`
	ComparisonOperator string      `json:"comparison_operator"`
	Value              interface{} `json:"value"`
}

type difyMetadataCondition struct {
	Conditions []difyCondition `json:"conditions"`
	Logic      string          `json:"logic"`
}

func (m *difyMetadataCondition) filter() (*common.MetaFilterInput, error) {
	if m == nil {
		return nil, nil
	}
	logic := m.Logic
	if logic == "" {
		logic = "and"
	}
	conditions := make([]common.MetaCondition, len(m.Conditions))
	for index, condition := range m.Conditions {
		conditions[index] = common.MetaCondition{Key: condition.Name, Operator: condition.ComparisonOperator, Value: condition.Value}
	}
	if !common.ValidMetaConditions(conditions, logic) {
		return nil, fmt.Errorf("invalid metadata condition")
	}
	if m.Conditions == nil {
		return nil, nil
	}
	// An explicitly empty array is a zero selection. Keep its marker distinct
	// from omitted conditions so the handler can stop after authorization.
	return &common.MetaFilterInput{Logic: logic, Conditions: conditions}, nil
}

type difyRecord struct {
	Content  string                 `json:"content"`
	Score    float64                `json:"score"`
	Title    string                 `json:"title"`
	Metadata map[string]interface{} `json:"metadata"`
}

func difyError(c *gin.Context, status int, code common.ErrorCode, message string) {
	c.JSON(status, gin.H{"error_code": code, "error_msg": message})
}

func decodeDifyRequest(c *gin.Context) (*difyRetrievalRequest, int, float64, *common.MetaFilterInput, error) {
	var request difyRetrievalRequest
	decoder := json.NewDecoder(c.Request.Body)
	var raw json.RawMessage
	if err := decoder.Decode(&raw); err != nil {
		return nil, 0, 0, nil, fmt.Errorf("invalid or malformed arguments")
	}
	if err := decoder.Decode(new(interface{})); !errors.Is(err, io.EOF) {
		return nil, 0, 0, nil, fmt.Errorf("invalid or malformed arguments")
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil || fields == nil {
		return nil, 0, 0, nil, fmt.Errorf("invalid or malformed arguments")
	}
	for _, name := range []string{"knowledge_id", "query", "use_kg", "retrieval_setting"} {
		if value, exists := fields[name]; exists && string(value) == "null" {
			return nil, 0, 0, nil, fmt.Errorf("invalid or malformed arguments")
		}
	}
	for container, names := range map[string][]string{"retrieval_setting": {"top_k", "score_threshold"}, "metadata_condition": {"conditions", "logic"}} {
		if value, exists := fields[container]; exists && string(value) != "null" {
			var nested map[string]json.RawMessage
			if err := json.Unmarshal(value, &nested); err != nil || nested == nil {
				return nil, 0, 0, nil, fmt.Errorf("invalid or malformed arguments")
			}
			for _, name := range names {
				if value, exists := nested[name]; exists && string(value) == "null" {
					return nil, 0, 0, nil, fmt.Errorf("invalid or malformed arguments")
				}
			}
			if container == "metadata_condition" {
				if value, exists := nested["logic"]; exists {
					var logic string
					if err := json.Unmarshal(value, &logic); err != nil || logic != "and" && logic != "or" {
						return nil, 0, 0, nil, fmt.Errorf("invalid metadata logic")
					}
				}
			}
		}
	}
	decoder = json.NewDecoder(strings.NewReader(string(raw)))
	decoder.UseNumber()
	if err := decoder.Decode(&request); err != nil {
		return nil, 0, 0, nil, fmt.Errorf("invalid or malformed arguments")
	}
	request.KnowledgeID = strings.TrimSpace(request.KnowledgeID)
	request.Query = strings.TrimSpace(request.Query)
	if request.KnowledgeID == "" || request.Query == "" {
		return nil, 0, 0, nil, fmt.Errorf("knowledge_id and query are required")
	}
	topK, threshold := 1024, 0.0
	if settings := request.RetrievalSetting; settings != nil {
		if settings.TopK != nil {
			topK = *settings.TopK
		}
		if settings.ScoreThreshold != nil {
			threshold = *settings.ScoreThreshold
		}
	}
	if topK < 1 || topK > 10000 || math.IsNaN(threshold) || math.IsInf(threshold, 0) || threshold < 0 || threshold > 1 {
		return nil, 0, 0, nil, fmt.Errorf("invalid retrieval settings")
	}
	// The current Go retriever has no search-mode contract. Reject an explicit
	// mode rather than report success while silently using a different search.
	if len(request.SearchMode) != 0 && string(request.SearchMode) != "null" {
		return nil, 0, 0, nil, fmt.Errorf("search_mode is unsupported by the Go retriever")
	}
	filter, err := request.MetadataCondition.filter()
	return &request, topK, threshold, filter, err
}

// Retrieval implements POST /api/v1/dify/retrieval. The route must use the
// Dify identity middleware; health is a separate dependency-free endpoint.
func (h *DifyRetrievalHandler) Retrieval(c *gin.Context) {
	if c.Request.Method != http.MethodPost {
		difyError(c, http.StatusMethodNotAllowed, common.CodeArgumentError, "use POST for retrieval")
		return
	}
	user, code, _ := GetUser(c)
	if code != common.CodeSuccess || user == nil || strings.TrimSpace(user.ID) == "" {
		difyError(c, http.StatusForbidden, common.CodeForbidden, "API-KEY is invalid or missing!")
		return
	}
	request, topK, threshold, filter, err := decodeDifyRequest(c)
	if err != nil {
		difyError(c, http.StatusBadRequest, common.CodeArgumentError, err.Error())
		return
	}
	ctx := c.Request.Context()
	if ctx.Err() != nil || h == nil || h.deps.Datasets == nil {
		difyError(c, http.StatusInternalServerError, common.CodeServerError, "retrieval is unavailable")
		return
	}
	kb, err := h.deps.Datasets.GetReadableByID(ctx, request.KnowledgeID, user.ID)
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			_, existenceError := h.deps.Datasets.GetByIDContext(ctx, request.KnowledgeID)
			if errors.Is(existenceError, gorm.ErrRecordNotFound) {
				difyError(c, http.StatusNotFound, common.CodeNotFound, "Knowledge base not found.")
				return
			}
			if existenceError != nil {
				difyError(c, http.StatusInternalServerError, common.CodeServerError, "failed to read knowledge base")
				return
			}
			difyError(c, http.StatusForbidden, common.CodeAuthenticationError, "No authorization for this knowledge base.")
		} else {
			difyError(c, http.StatusInternalServerError, common.CodeServerError, "failed to read knowledge base")
		}
		return
	}
	if kb == nil || kb.ID != request.KnowledgeID || kb.TenantID == "" {
		difyError(c, http.StatusForbidden, common.CodeAuthenticationError, "No authorization for this knowledge base.")
		return
	}
	if tokenTenant, exists := c.Get(difyTokenTenantContext); exists && tokenTenant != kb.TenantID {
		difyError(c, http.StatusForbidden, common.CodeAuthenticationError, "No authorization for this knowledge base.")
		return
	}
	if err := ctx.Err(); err != nil {
		difyError(c, http.StatusInternalServerError, common.CodeServerError, "retrieval cancelled")
		return
	}
	if filter != nil && len(filter.Conditions) == 0 {
		c.JSON(http.StatusOK, gin.H{"records": []difyRecord{}})
		return
	}
	var docIDs []string
	if filter != nil {
		if h.deps.Metadata == nil {
			difyError(c, http.StatusInternalServerError, common.CodeServerError, "metadata is unavailable")
			return
		}
		metadata, err := h.deps.Metadata.GetFlattedMetaByKBsContext(ctx, []string{kb.ID})
		if err != nil || ctx.Err() != nil {
			difyError(c, http.StatusInternalServerError, common.CodeServerError, "failed to read metadata")
			return
		}
		docIDs = common.MetaFilter(metadata, filter)
		if len(docIDs) == 0 {
			c.JSON(http.StatusOK, gin.H{"records": []difyRecord{}})
			return
		}
	}
	if h.deps.Models == nil || h.deps.Metadata == nil || h.deps.Retrieval == nil || h.deps.Documents == nil || h.deps.Children == nil || request.UseKG && h.deps.KG == nil {
		difyError(c, http.StatusInternalServerError, common.CodeServerError, "retrieval dependencies are unavailable")
		return
	}
	embedding, err := h.deps.Models.Embedding(ctx, kb)
	if err != nil || embedding == nil {
		difyError(c, http.StatusInternalServerError, common.CodeServerError, "failed to bind embedding model")
		return
	}
	rank, err := h.deps.Metadata.LabelQuestionContext(ctx, user.ID, request.Query, []*entity.Knowledgebase{kb})
	if err != nil {
		difyError(c, http.StatusInternalServerError, common.CodeServerError, "failed to read ranking features")
		return
	}
	search := &nlp.RetrievalRequest{
		Question: request.Query, TenantIDs: []string{kb.TenantID}, KbIDs: []string{kb.ID},
		DocIDs: docIDs, Page: 1, PageSize: topK, Top: &topK, SimilarityThreshold: &threshold,
		EmbeddingModel: embedding,
	}
	if rank != nil {
		search.RankFeature = &rank
	}
	result, err := h.deps.Retrieval.Retrieval(ctx, search)
	if err != nil || result == nil {
		difyError(c, http.StatusInternalServerError, common.CodeServerError, "chunk retrieval failed")
		return
	}
	chunks, err := h.liveDifyChunks(ctx, result.Chunks, kb.ID, docIDs)
	if err != nil {
		difyError(c, http.StatusInternalServerError, common.CodeServerError, "failed to read source documents")
		return
	}
	chunks, err = h.deps.Children(ctx, chunks, kb.TenantID)
	if err != nil {
		difyError(c, http.StatusInternalServerError, common.CodeServerError, "child retrieval failed")
		return
	}
	// Verify ordinary source documents after child expansion and before metadata
	// reads. KG is a separate trusted result, never an empty-doc chunk exception.
	chunks, err = h.liveDifyChunks(ctx, chunks, kb.ID, docIDs)
	if err != nil {
		difyError(c, http.StatusInternalServerError, common.CodeServerError, "failed to read source documents")
		return
	}
	chunks, err = h.deps.Metadata.EnrichChunksWithDocMetadata(ctx, chunks, user.ID, nil)
	if err != nil {
		difyError(c, http.StatusInternalServerError, common.CodeServerError, "failed to enrich document metadata")
		return
	}
	records := make([]difyRecord, 0, len(chunks)+1)
	if request.UseKG {
		chat, err := h.deps.Models.Chat(ctx, kb)
		if err != nil || chat == nil {
			difyError(c, http.StatusInternalServerError, common.CodeServerError, "failed to bind chat model")
			return
		}
		graph, err := h.deps.KG.Retrieval(ctx, user.ID, []string{kb.ID}, request.Query, chat, embedding)
		if err != nil || graph == nil || graph.KBID != kb.ID {
			difyError(c, http.StatusInternalServerError, common.CodeServerError, "knowledge graph retrieval failed")
			return
		}
		if graph.Content != "" {
			records = append(records, difyRecord{Content: graph.Content, Score: 1, Title: "Knowledge Graph", Metadata: map[string]interface{}{"doc_id": "", "document_id": ""}})
		}
	}
	for _, chunk := range chunks {
		docID := difyIdentity(chunk["doc_id"])
		if docID == "" || difyIdentity(chunk["kb_id"]) != kb.ID {
			continue
		}
		score, valid := difyScore(chunk["similarity"])
		if !valid {
			difyError(c, http.StatusInternalServerError, common.CodeServerError, "invalid retrieval score")
			return
		}
		metadata := make(map[string]interface{})
		if fields, ok := chunk["document_metadata"].(map[string]interface{}); ok {
			for key, value := range fields {
				metadata[key] = value
			}
		}
		metadata["doc_id"], metadata["document_id"] = docID, docID
		content, _ := chunk["content_with_weight"].(string)
		title, _ := chunk["docnm_kwd"].(string)
		records = append(records, difyRecord{Content: content, Score: score, Title: title, Metadata: metadata})
	}
	if err := ctx.Err(); err != nil {
		difyError(c, http.StatusInternalServerError, common.CodeServerError, "retrieval cancelled")
		return
	}
	c.JSON(http.StatusOK, gin.H{"records": records})
}

func (h *DifyRetrievalHandler) liveDifyChunks(ctx context.Context, chunks []map[string]interface{}, kbID string, selection []string) ([]map[string]interface{}, error) {
	selected := make(map[string]bool, len(selection))
	for _, id := range selection {
		selected[id] = true
	}
	ids := make(map[string]bool)
	for _, chunk := range chunks {
		docID := difyIdentity(chunk["doc_id"])
		if docID != "" && difyIdentity(chunk["kb_id"]) == kbID && (selection == nil || selected[docID]) {
			ids[docID] = true
		}
	}
	ordered := make([]string, 0, len(ids))
	for id := range ids {
		ordered = append(ordered, id)
	}
	sort.Strings(ordered)
	live := make(map[string]bool, len(ids))
	if len(ordered) > 0 {
		documents, err := h.deps.Documents.GetByIDs(ctx, ordered)
		if err != nil {
			return nil, err
		}
		for _, document := range documents {
			if document != nil && document.KbID == kbID && ids[document.ID] {
				live[document.ID] = true
			}
		}
	}
	result := make([]map[string]interface{}, 0, len(chunks))
	for _, chunk := range chunks {
		if live[difyIdentity(chunk["doc_id"])] && difyIdentity(chunk["kb_id"]) == kbID {
			result = append(result, chunk)
		}
	}
	return result, ctx.Err()
}

func difyIdentity(value interface{}) string {
	switch value := value.(type) {
	case string:
		return value
	case []string:
		if len(value) == 1 {
			return value[0]
		}
	case []interface{}:
		if len(value) == 1 {
			text, _ := value[0].(string)
			return text
		}
	}
	return ""
}

func difyScore(value interface{}) (float64, bool) {
	if value == nil {
		return 0, true
	}
	var score float64
	switch value := value.(type) {
	case float64:
		score = value
	case float32:
		score = float64(value)
	case int:
		score = float64(value)
	case json.Number:
		parsed, err := value.Float64()
		if err != nil {
			return 0, false
		}
		score = parsed
	default:
		return 0, false
	}
	return score, !math.IsNaN(score) && !math.IsInf(score, 0)
}

// HealthCheck reports reachability without resolving identity or dependencies.
func (h *DifyRetrievalHandler) HealthCheck(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"code": 0, "data": true})
}
