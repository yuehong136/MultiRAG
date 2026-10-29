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

package nlp

import (
	"context"
	"fmt"
	"math"
	"multirag/internal/common"
	"sort"
	"strings"

	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/tokenizer"

	"go.uber.org/zap"
)

// RetrievalService provides retrieval search functionality
type RetrievalService struct {
	docEngine   engine.DocEngine
	documentDAO documentReader
	kbDAO       knowledgebaseReader
}

type documentReader interface {
	GetByIDs(context.Context, []string) ([]*entity.Document, error)
}

type knowledgebaseReader interface {
	GetExistingIDs(context.Context, []string) ([]string, error)
}

// NewRetrievalService creates a new RetrievalService with the given doc engine
func NewRetrievalService(docEngine engine.DocEngine) *RetrievalService {
	return &RetrievalService{docEngine: docEngine, documentDAO: dao.NewDocumentDAO(), kbDAO: dao.NewKnowledgebaseDAO()}
}

// RetrievalRequest request for retrieval search
type RetrievalRequest struct {
	Question               string
	TenantIDs              []string
	KbIDs                  []string
	DocIDs                 []string
	Page                   int
	PageSize               int
	Top                    *int
	SimilarityThreshold    *float64
	VectorSimilarityWeight *float64
	RankFeature            *map[string]float64
	RerankModel            *models.RerankModel
	EmbeddingModel         *models.EmbeddingModel
	Aggs                   *bool
	Highlight              *bool
}

// RetrievalResult result from retrieval search
type RetrievalResult struct {
	Total   int64
	Chunks  []map[string]interface{}
	DocAggs []map[string]interface{} // Aggregated document counts, sorted by count desc
}

// Retrieval performs hybrid search + reranking + pagination
// - Calculate rerank limit and call Search() to fetch rerankLimit candidates for reranking
// - Perform reranking via Rerank()
// - Sort indices by score descending and filter by threshold
// - Calculate pagination to extract actual page returned from reranked results
// - Build chunks
// - Build document aggregation if specified
func (s *RetrievalService) Retrieval(ctx context.Context, req *RetrievalRequest) (*RetrievalResult, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if req.Question == "" || (req.DocIDs != nil && len(req.DocIDs) == 0) {
		return &RetrievalResult{Chunks: []map[string]interface{}{}, DocAggs: []map[string]interface{}{}}, nil
	}

	// Apply default values
	if req.Top == nil {
		req.Top = func() *int { v := 1024; return &v }()
	}
	if req.SimilarityThreshold == nil {
		req.SimilarityThreshold = func() *float64 { v := 0.0; return &v }()
	}
	if req.VectorSimilarityWeight == nil {
		req.VectorSimilarityWeight = func() *float64 { v := 0.3; return &v }()
	}
	if req.RankFeature == nil {
		req.RankFeature = &map[string]float64{"pagerank_fea": 10.0}
	}
	if req.Aggs == nil {
		req.Aggs = func() *bool { v := true; return &v }()
	}

	if req.Page <= 0 || req.PageSize <= 0 {
		return nil, fmt.Errorf("page and page size must be positive")
	}

	if *req.Top < 0 {
		return nil, fmt.Errorf("top must be non-negative")
	}
	if err := validateRerankWeights(1-*req.VectorSimilarityWeight, *req.VectorSimilarityWeight); err != nil {
		return nil, err
	}
	if threshold := *req.SimilarityThreshold; math.IsNaN(threshold) || math.IsInf(threshold, 0) || threshold < 0 || threshold > 1 {
		return nil, fmt.Errorf("similarity threshold must be finite and between 0 and 1")
	}
	// Fetch and in-block slicing must use exactly the same page-aligned window.
	pageSize := req.PageSize
	top := 0
	if req.RerankModel != nil {
		top = *req.Top
	}
	rerankLimit := types.RerankWindow(pageSize, top)
	if req.Page-1 > int(^uint(0)>>1)/pageSize {
		return nil, fmt.Errorf("page offset is too large")
	}

	page := req.Page
	globalOffset := (page - 1) * pageSize
	searchPage := globalOffset/rerankLimit + 1
	common.Debug("Retrieval rerank params", zap.Int("page", req.Page), zap.Int("pageSize", pageSize),
		zap.Int("searchPage", searchPage), zap.Int("rerankLimit", rerankLimit), zap.Int("globalOffset", globalOffset))

	// Execute search via Search()
	searchReq := &RetrievalSearchRequest{
		TenantIDs:      req.TenantIDs,
		Question:       req.Question,
		KbIDs:          req.KbIDs,
		DocIDs:         req.DocIDs,
		Page:           searchPage,
		PageSize:       rerankLimit,
		Top:            *req.Top,
		RankFeature:    *req.RankFeature,
		EmbeddingModel: req.EmbeddingModel,
	}
	searchResult, err := s.Search(ctx, searchReq)
	if err != nil {
		return nil, fmt.Errorf("Search failed: %w", err)
	}

	searchResult, err = s.pruneDeletedChunks(ctx, searchResult, req.KbIDs)
	if err != nil {
		return nil, fmt.Errorf("prune deleted chunks: %w", err)
	}
	if len(searchResult.IDs) == 0 {
		return &RetrievalResult{Total: searchResult.Total, Chunks: []map[string]interface{}{}, DocAggs: []map[string]interface{}{}}, nil
	}

	// Perform reranking
	vtWeight := *req.VectorSimilarityWeight
	tkWeight := 1.0 - vtWeight
	qb := GetQueryBuilder()
	useInfinity := engine.GetEngineType() == engine.EngineInfinity
	sim, termSimilarity, vectorSimilarity, err := Rerank(
		ctx,
		req.RerankModel,
		searchResult.Chunks,
		int(searchResult.Total),
		nil,
		searchResult.QueryVector,
		req.Question,
		tkWeight,
		vtWeight,
		useInfinity,
		"content_ltks",
		qb,
		*req.RankFeature,
	)
	if err != nil {
		return nil, fmt.Errorf("rerank: %w", err)
	}
	if len(sim) == 0 {
		return &RetrievalResult{Total: searchResult.Total, Chunks: []map[string]interface{}{}, DocAggs: []map[string]interface{}{}}, nil
	}

	// Sort indices (positions into search results) by score descending
	// After sorting by score descending, we process chunks in relevance order
	type idxScore struct {
		idx   int
		score float64
	}
	idxScores := make([]idxScore, 0, len(sim))
	for i, s := range sim {
		idxScores = append(idxScores, idxScore{idx: i, score: s})
	}
	sort.SliceStable(idxScores, func(i, j int) bool {
		return idxScores[i].score > idxScores[j].score
	})

	// When vector_similarity_weight is 0, similarity_threshold is not meaningful for term-only scores
	postThreshold := *req.SimilarityThreshold
	if *req.VectorSimilarityWeight <= 0 {
		postThreshold = 0.0
	}

	// Get valid indices where score >= postThreshold
	validIdx := make([]int, 0)
	for _, is := range idxScores {
		if is.score >= postThreshold {
			validIdx = append(validIdx, is.idx)
		}
	}
	if len(validIdx) == 0 {
		return &RetrievalResult{Total: searchResult.Total, Chunks: []map[string]interface{}{}, DocAggs: []map[string]interface{}{}}, nil
	}

	// Calculate pagination
	// begin and end define which of validIdx to return as the page
	begin := globalOffset % rerankLimit
	end := begin + pageSize

	// Get page indices
	var pageIdx []int
	if begin < len(validIdx) {
		if end > len(validIdx) {
			end = len(validIdx)
		}
		pageIdx = validIdx[begin:end]
	}
	common.Debug("Pagination result info", zap.Int("totalValid", len(validIdx)), zap.Int("begin", begin),
		zap.Int("end", end), zap.Int("chunkCount", len(pageIdx)))

	// Build chunks for pageIdx, transforms raw search results into the API response format
	var filteredChunks []map[string]interface{}
	dim := 0
	if searchResult.QueryVector != nil {
		dim = len(searchResult.QueryVector)
	}
	zeroVector := make([]float64, dim)
	for j := 0; j < dim; j++ {
		zeroVector[j] = 0.0
	}

	for _, i := range pageIdx {
		if i < 0 || i >= len(searchResult.IDs) {
			continue
		}
		chunkID := searchResult.IDs[i]
		chunk, exists := searchResult.Field[chunkID]
		if !exists {
			continue
		}

		resultChunk := make(map[string]interface{})
		resultChunk["chunk_id"] = chunkID
		if v, ok := chunk["content_ltks"]; ok {
			resultChunk["content_ltks"] = v
		}
		if v, ok := chunk["content_with_weight"]; ok {
			resultChunk["content_with_weight"] = v
		}
		if v, ok := chunk["doc_id"]; ok {
			resultChunk["doc_id"] = v
		}
		if v, ok := chunk["docnm_kwd"]; ok {
			resultChunk["docnm_kwd"] = v
		}
		if v, ok := chunk["kb_id"]; ok {
			resultChunk["kb_id"] = v
		}
		if v, ok := chunk["important_kwd"]; ok {
			resultChunk["important_kwd"] = v
		}
		if v, ok := chunk["tag_kwd"]; ok {
			resultChunk["tag_kwd"] = v
		}
		if v, ok := chunk["img_id"]; ok {
			resultChunk["image_id"] = v
		}
		if v, ok := chunk["position_int"]; ok {
			resultChunk["positions"] = v
		}
		if v, ok := chunk["doc_type_kwd"]; ok {
			resultChunk["doc_type_kwd"] = v
		}
		if v, ok := chunk["mom_id"]; ok {
			resultChunk["mom_id"] = v
		}
		// row_id: row identifier (for structured data like tables)
		if v, ok := chunk["row_id()"]; ok {
			resultChunk["row_id"] = v
		}
		resultChunk["similarity"] = sim[i]
		resultChunk["term_similarity"] = termSimilarity[i]
		resultChunk["vector_similarity"] = vectorSimilarity[i]
		vectorColumn := fmt.Sprintf("q_%d_vec", dim)
		if v, ok := chunk[vectorColumn]; ok {
			resultChunk["vector"] = v
		} else {
			resultChunk["vector"] = zeroVector
		}

		highlightEnabled := false
		if req.Highlight != nil && *req.Highlight {
			highlightEnabled = true
		}
		if highlightEnabled && searchResult.Highlight != nil {
			if highlightText, ok := searchResult.Highlight[chunkID]; ok {
				resultChunk["highlight"] = RemoveRedundantSpaces(highlightText)
			} else if contentWithWeight, ok := chunk["content_with_weight"].(string); ok {
				resultChunk["highlight"] = RemoveRedundantSpaces(contentWithWeight)
			}
		}
		filteredChunks = append(filteredChunks, resultChunk)
	}

	// Build document aggregation, aggregates document-level statistics across all valid chunks
	// This is useful for showing users which documents are most relevant to their query.
	var docAggs []map[string]interface{}
	if req.Aggs != nil && *req.Aggs {
		type docAggKey struct{ docName, docID, kbID string }
		docAggsMap := make(map[docAggKey]struct {
			docID string
			count int
		})
		for _, i := range validIdx {
			if i < 0 || i >= len(searchResult.IDs) {
				continue
			}
			chunkID := searchResult.IDs[i]
			chunk, exists := searchResult.Field[chunkID]
			if !exists {
				continue
			}
			docName := ""
			docID := ""
			if v, ok := chunk["docnm_kwd"].(string); ok {
				docName = v
			}
			if v, ok := chunk["doc_id"].(string); ok {
				docID = v
			}
			key := docAggKey{docName: docName, docID: docID, kbID: chunkScalar(chunk["kb_id"])}
			if entry, exists := docAggsMap[key]; exists {
				entry.count++
				docAggsMap[key] = entry
			} else {
				docAggsMap[key] = struct {
					docID string
					count int
				}{docID: docID, count: 1}
			}
		}

		// Sort by count descending
		type docAggEntry struct {
			docName string
			docID   string
			count   int
		}
		docAggsList := make([]docAggEntry, 0, len(docAggsMap))
		for key, entry := range docAggsMap {
			docAggsList = append(docAggsList, docAggEntry{docName: key.docName, docID: entry.docID, count: entry.count})
		}
		sort.Slice(docAggsList, func(i, j int) bool {
			if docAggsList[i].count == docAggsList[j].count {
				if docAggsList[i].docName == docAggsList[j].docName {
					return docAggsList[i].docID < docAggsList[j].docID
				}
				return docAggsList[i].docName < docAggsList[j].docName
			}
			return docAggsList[i].count > docAggsList[j].count
		})

		docAggs = make([]map[string]interface{}, 0, len(docAggsList))
		for _, entry := range docAggsList {
			docAggs = append(docAggs, map[string]interface{}{
				"doc_name": entry.docName,
				"doc_id":   entry.docID,
				"count":    entry.count,
			})
		}
	} else {
		docAggs = []map[string]interface{}{}
	}

	return &RetrievalResult{
		Total:   searchResult.Total,
		Chunks:  filteredChunks,
		DocAggs: docAggs,
	}, nil
}

// RetrievalSearchRequest is the request struct for RetrievalService.Search()
type RetrievalSearchRequest struct {
	Question            string
	TenantIDs           []string
	KbIDs               []string
	DocIDs              []string
	Top                 int
	Page                int
	PageSize            int
	Sort                bool
	Highlight           *bool
	SimilarityThreshold float64
	RankFeature         map[string]float64
	Filter              map[string]interface{}
	EmbeddingModel      *models.EmbeddingModel
}

type RetrievalSearchResult struct {
	Chunks      []map[string]interface{}          // Search results
	Total       int64                             // Total number of matches
	QueryVector []float64                         // Query vector (for hybrid search, used in reranking)
	Highlight   map[string]string                 // Highlighted snippets (chunk_id -> highlighted text)
	Field       map[string]map[string]interface{} // ID -> chunk mapping
	IDs         []string                          // Ordered list of chunk IDs
	Keywords    []string                          // Keywords from query
	Aggregation []map[string]interface{}          // Doc aggregation by field
	Options     map[string]interface{}            // Engine-specific options (e.g., total from get_total)
}

// Search performs search based on question and EmbeddingModel:
// - Empty question: list data matching filters, optionally sorted
// - Non-empty question, no EmbeddingModel: fulltext search only
// - Non-empty question, with EmbeddingModel: hybrid search (fulltext + vector + fusion)
//
// Hybrid search path retries with lower thresholds if no results found.
func (s *RetrievalService) Search(ctx context.Context, req *RetrievalSearchRequest) (*RetrievalSearchResult, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if req.DocIDs != nil && len(req.DocIDs) == 0 {
		return &RetrievalSearchResult{Chunks: []map[string]interface{}{}, Field: map[string]map[string]interface{}{}, IDs: []string{}}, nil
	}
	if req.Highlight == nil {
		req.Highlight = func() *bool { v := false; return &v }()
	}
	filters := req.GetFilters()
	pg := req.Page - 1
	if pg < 0 {
		pg = 0
	}
	topk := req.Top
	if topk <= 0 {
		topk = 1024
	}
	pageSize := req.PageSize
	if pageSize <= 0 {
		pageSize = topk
	}
	limit := pageSize

	// Build Source field list
	src := []string{
		"id", "docnm_kwd", "content_ltks", "kb_id", "img_id", "title_tks", "important_kwd", "position_int",
		"doc_id", "chunk_order_int", "page_num_int", "top_int", "create_timestamp_flt", "knowledge_graph_kwd",
		"question_kwd", "question_tks", "doc_type_kwd",
		"available_int", "content_with_weight", "mom_id", "pagerank_fea", "tag_feas",
		"raptor_kwd",
	}
	if engine.GetEngineType() == engine.EngineInfinity {
		src = append(src, "row_id()")
	}

	kwds := make(map[string]struct{})

	// Build base engine request with common fields
	// Note: RankFeature is NOT set here, it's set per-call where needed
	searchRequest := &types.SearchRequest{
		IndexNames:   buildIndexNames(req.TenantIDs),
		KbIDs:        req.KbIDs,
		Offset:       pg * pageSize,
		Limit:        limit,
		Filter:       filters,
		SelectFields: src,
	}

	// engineResult holds the result from docEngine.Search() (types.SearchResult)
	// queryVector tracks the query vector for reranking
	var engineResult *types.SearchResult
	var queryVector []float64
	var err error

	if req.Question == "" {
		// Empty question
		if req.Sort {
			searchRequest.OrderBy = &types.OrderByExpr{}
			searchRequest.OrderBy.Asc("chunk_order_int").Asc("page_num_int").Asc("top_int").Desc("create_timestamp_flt")
		}
		searchRequest.MatchExprs = []interface{}{}
		engineResult, err = s.docEngine.Search(ctx, searchRequest)
		if err != nil {
			return nil, fmt.Errorf("Search failed: %w", err)
		}
	} else {
		// Non-empty question

		// Compute keywords via QueryBuilder
		matchText, keywords := GetQueryBuilder().Question(req.Question, "", 0.3)
		for _, k := range keywords {
			kwds[k] = struct{}{}
		}

		// Check if EmbeddingModel is available
		if req.EmbeddingModel == nil {
			// Keyword-only search
			searchRequestWithRank := *searchRequest
			searchRequestWithRank.MatchExprs = []interface{}{matchText}
			searchRequestWithRank.RankFeature = req.RankFeature

			engineResult, err = s.docEngine.Search(ctx, &searchRequestWithRank)
			if err != nil {
				return nil, fmt.Errorf("Search failed: %w", err)
			}
			queryVector = nil
		} else {
			// Compute question vector via GetVector
			similarityForGetVector := req.SimilarityThreshold
			if similarityForGetVector <= 0 {
				similarityForGetVector = 0.1
			}
			matchDense, err := s.GetVector(req.Question, req.EmbeddingModel, topk, similarityForGetVector)
			if err != nil {
				return nil, fmt.Errorf("GetVector failed: %w", err)
			}

			// Execute search with fusion
			fusionExpr := &types.FusionExpr{
				Method:       "weighted_sum",
				TopN:         topk,
				FusionParams: map[string]interface{}{"weights": "0.05,0.95"},
			}

			// Build source with vector column for ES
			searchSrc := make([]string, len(searchRequest.SelectFields))
			copy(searchSrc, searchRequest.SelectFields)
			if engine.GetEngineType() == engine.EngineElasticsearch {
				searchSrc = append(searchSrc, matchDense.VectorColumnName)
			}

			searchRequest.SelectFields = searchSrc
			searchRequest.MatchExprs = []interface{}{matchText, matchDense, fusionExpr}
			searchRequest.RankFeature = req.RankFeature

			engineResult, err = s.docEngine.Search(ctx, searchRequest)
			if err != nil {
				return nil, fmt.Errorf("Search failed: %w", err)
			}
			// If result is empty, retry with lower min_match
			if engineResult.Total == 0 {
				_, hasDocIDFilter := filters["doc_id"]
				if hasDocIDFilter {
					// Fallback without vector query when doc_id filter is present
					searchRequest.SelectFields = src
					searchRequest.MatchExprs = []interface{}{}
					searchRequest.RankFeature = nil

					engineResult, err = s.docEngine.Search(ctx, searchRequest)
					if err != nil {
						return nil, fmt.Errorf("Search retry failed: %w", err)
					}
				} else {
					// Retry with lower min_match via QueryBuilder
					matchText, _ := GetQueryBuilder().Question(req.Question, "qa", 0.1)
					matchDense.ExtraOptions["similarity"] = 0.17
					searchRequest.MatchExprs = []interface{}{matchText, matchDense, fusionExpr}
					searchRequest.RankFeature = req.RankFeature

					engineResult, err = s.docEngine.Search(ctx, searchRequest)
					if err != nil {
						return nil, fmt.Errorf("Search retry failed: %w", err)
					}
				}
			}

			queryVector = matchDense.EmbeddingData
		}

		// Build kwds from keywords with fine-grained tokenization
		for _, k := range keywords {
			kwds[k] = struct{}{}
			fgToken, _ := tokenizer.FineGrainedTokenize(k)
			for _, kk := range strings.Fields(fgToken) {
				if len([]rune(kk)) < 2 {
					continue
				}
				if _, ok := kwds[kk]; ok {
					continue
				}
				kwds[kk] = struct{}{}
			}
		}
	}

	searchResult := engineResult
	ids := s.docEngine.GetDocIDs(searchResult.Chunks)

	// Build Keywords list from kwds set
	keywordsList := make([]string, 0, len(kwds))
	for k := range kwds {
		keywordsList = append(keywordsList, k)
	}

	sort.Strings(keywordsList)

	// Build Field map
	fieldMap := s.docEngine.GetFields(searchResult.Chunks, nil)

	// Build Aggregation
	aggregation := s.docEngine.GetAggregation(searchResult.Chunks, "docnm_kwd")

	// Build Highlight using GetHighlight
	var highlight map[string]string
	if len(keywordsList) > 0 {
		highlight = s.docEngine.GetHighlight(searchResult.Chunks, keywordsList, "content_with_weight")
	}

	return &RetrievalSearchResult{
		Chunks:      searchResult.Chunks,
		Total:       searchResult.Total,
		QueryVector: queryVector,
		Highlight:   highlight,
		Field:       fieldMap,
		IDs:         ids,
		Keywords:    keywordsList,
		Aggregation: aggregation,
	}, nil
}

// GetVector computes query vector and returns MatchDenseExpr for hybrid search
func (s *RetrievalService) GetVector(txt string, embModel *models.EmbeddingModel, topk int, similarity float64) (*types.MatchDenseExpr, error) {
	if topk < 0 {
		return nil, fmt.Errorf("top must be non-negative")
	}
	if topk == 0 {
		topk = 1024
	}
	queryModel := *embModel
	config := models.EmbeddingConfig{Query: true}
	if embModel.EmbeddingConfig != nil {
		config = *embModel.EmbeddingConfig
		config.Query = true
	}
	queryModel.EmbeddingConfig = &config
	embeddings, err := queryModel.Embed([]string{txt})
	if err != nil {
		return nil, err
	}

	// Bound Embed validates exactly one nonempty vector before indexing.
	vector := embeddings[0].Embedding
	vectorSize := len(vector)
	vectorColumnName := fmt.Sprintf("q_%d_vec", vectorSize)

	return &types.MatchDenseExpr{
		VectorColumnName:  vectorColumnName,
		EmbeddingData:     vector,
		EmbeddingDataType: "float",
		DistanceType:      "cosine",
		TopN:              topk,
		ExtraOptions:      map[string]interface{}{"similarity": similarity},
	}, nil
}

// GetFilters builds metadata filter map from RetrievalSearchRequest
func (r *RetrievalSearchRequest) GetFilters() map[string]interface{} {
	filters := make(map[string]interface{})

	if len(r.KbIDs) > 0 {
		filters["kb_id"] = r.KbIDs
	}
	if len(r.DocIDs) > 0 {
		filters["doc_id"] = r.DocIDs
	}
	for _, key := range []string{"knowledge_graph_kwd", "available_int", "entity_kwd", "from_entity_kwd", "to_entity_kwd", "removed_kwd"} {
		if val, ok := r.Filter[key]; ok && val != nil {
			filters[key] = val
		}
	}
	for key, val := range r.Filter {
		if _, exists := filters[key]; !exists && val != nil {
			filters[key] = val
		}
	}
	return filters
}

// RetrievalByChildrenChecked expands verified children without hiding storage failures.
func RetrievalByChildrenChecked(ctx context.Context, chunks []map[string]interface{}, tenantIDs []string, docEngine engine.DocEngine) ([]map[string]interface{}, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	common.Info("RetrievalByChildren started", zap.Int("chunks", len(chunks)), zap.Strings("tenantIDs", tenantIDs))

	indexNames := buildIndexNames(tenantIDs)
	if len(chunks) == 0 || len(indexNames) == 0 {
		return chunks, nil
	}

	// Group children by parent ID and their verified SQL document/dataset.
	type childChunk struct {
		chunk    map[string]interface{}
		position int
	}
	type parentKey struct {
		momID string
		docID string
		kbID  string
	}
	momChunks := make(map[parentKey][]childChunk)
	var keyOrder []parentKey
	firstPosition := make(map[parentKey]int)
	var output []childChunk

	for position, ck := range chunks {
		momID, ok := ck["mom_id"].(string)
		if !ok || momID == "" {
			output = append(output, childChunk{chunk: ck, position: position})
			continue
		}
		key := parentKey{momID: momID, docID: chunkScalar(ck["doc_id"]), kbID: chunkScalar(ck["kb_id"])}
		if _, exists := momChunks[key]; !exists {
			keyOrder = append(keyOrder, key)
			firstPosition[key] = position
		}
		momChunks[key] = append(momChunks[key], childChunk{chunk: ck, position: position})
	}

	if len(momChunks) == 0 {
		common.Info("RetrievalByChildren finished", zap.Int("momChunks", len(momChunks)), zap.Int("resultChunks", len(chunks)))
		return chunks, nil
	}

	// Fetch parent chunks and aggregate
	for _, key := range keyOrder {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		childList := momChunks[key]
		momID := key.momID
		kbIDs := []string{key.kbID}

		if key.docID == "" || key.kbID == "" {
			return nil, fmt.Errorf("parent expansion requires document and dataset scope")
		}
		var parentMap map[string]interface{}
		for _, indexName := range indexNames {
			parent, err := docEngine.GetChunk(ctx, indexName, momID, kbIDs)
			if err != nil {
				return nil, fmt.Errorf("get parent chunk: %w", err)
			}
			if err := ctx.Err(); err != nil {
				return nil, err
			}
			if parent == nil {
				continue
			}
			var ok bool
			parentMap, ok = parent.(map[string]interface{})
			if !ok {
				return nil, fmt.Errorf("parent chunk has invalid shape")
			}
			if chunkScalar(parentMap["doc_id"]) != key.docID || chunkScalar(parentMap["kb_id"]) != key.kbID {
				return nil, fmt.Errorf("parent chunk does not match document and dataset scope")
			}
			break
		}
		if parentMap == nil {
			continue
		}

		// Calculate average similarity
		var totalSim float64
		for _, c := range childList {
			if sim, ok := c.chunk["similarity"].(float64); ok {
				totalSim += sim
			}
		}
		avgSim := totalSim / float64(len(childList))

		// Collect content_ltks from children
		var contentParts []string
		for _, c := range childList {
			if ltks, ok := c.chunk["content_ltks"].(string); ok {
				contentParts = append(contentParts, ltks)
			}
		}
		contentLTKS := strings.Join(contentParts, " ")

		// Collect important_kwd from children
		allImportantKwd := []string{}
		for _, c := range childList {
			if kwd, ok := c.chunk["important_kwd"].([]interface{}); ok {
				for _, k := range kwd {
					if ks, ok := k.(string); ok {
						allImportantKwd = append(allImportantKwd, ks)
					}
				}
			}
		}

		// Build aggregated chunk
		docTypeKwd := parentMap["doc_type_kwd"]
		if v, ok := docTypeKwd.(string); ok && v == "" {
			docTypeKwd = []interface{}{}
		}
		aggregated := map[string]interface{}{
			"chunk_id":            momID,
			"content_ltks":        contentLTKS,
			"content_with_weight": parentMap["content_with_weight"],
			"doc_id":              parentMap["doc_id"],
			"docnm_kwd":           parentMap["docnm_kwd"],
			"kb_id":               parentMap["kb_id"],
			"important_kwd":       allImportantKwd,
			"image_id":            parentMap["img_id"],
			"similarity":          avgSim,
			"vector_similarity":   avgSim,
			"term_similarity":     avgSim,
			"vector":              []float64{},
			"positions":           parentMap["position_int"],
			"doc_type_kwd":        docTypeKwd,
		}

		// Child API results expose the selected embedding as vector. Preserve it
		// without inferring a dimension from an unrelated parent group.
		for _, child := range childList {
			if vector, ok := child.chunk["vector"].([]float64); ok {
				aggregated["vector"] = vector
				break
			}
		}

		output = append(output, childChunk{chunk: aggregated, position: firstPosition[key]})
	}

	// Stable score ties retain the original candidate order, including parents.
	sort.SliceStable(output, func(i, j int) bool {
		left, _ := output[i].chunk["similarity"].(float64)
		right, _ := output[j].chunk["similarity"].(float64)
		if left == right {
			return output[i].position < output[j].position
		}
		return left > right
	})
	remainingChunks := make([]map[string]interface{}, 0, len(output))
	for _, item := range output {
		remainingChunks = append(remainingChunks, item.chunk)
	}

	common.Info("RetrievalByChildren finished", zap.Int("momChunks", len(momChunks)), zap.Int("resultChunks", len(remainingChunks)))
	return remainingChunks, nil
}

// chunkScalar accepts the scalar and singleton-list forms used by index engines.
func chunkScalar(value interface{}) string {
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

func isDatasetRaptorChunk(chunk map[string]interface{}) bool {
	return chunkScalar(chunk["doc_id"]) == "graph_raptor_x" &&
		chunkScalar(chunk["raptor_kwd"]) == "raptor" && chunkScalar(chunk["kb_id"]) != ""
}

// pruneDeletedChunks checks each candidate's SQL parent before its text reaches
// scoring. Dataset RAPTOR summaries use a live selected dataset as their parent;
// file RAPTOR uses a document. Explicit KG retrieval keeps its separate lifecycle.
// Keep the backend total and candidate-window offsets; pruning can yield short pages.
func (s *RetrievalService) pruneDeletedChunks(ctx context.Context, result *RetrievalSearchResult, kbIDs []string) (*RetrievalSearchResult, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	docIDs, summaryKBIDs := []string{}, []string{}
	seenDocs, seenKBs := map[string]bool{}, map[string]bool{}
	selectedKBs := map[string]bool{}
	for _, id := range kbIDs {
		selectedKBs[id] = true
	}
	for _, id := range result.IDs {
		chunk := result.Field[id]
		kbID := chunkScalar(chunk["kb_id"])
		if chunk == nil || (len(kbIDs) > 0 && !selectedKBs[kbID]) {
			continue
		}
		if isDatasetRaptorChunk(chunk) {
			if selectedKBs[kbID] && !seenKBs[kbID] {
				seenKBs[kbID] = true
				summaryKBIDs = append(summaryKBIDs, kbID)
			}
		} else if docID := chunkScalar(chunk["doc_id"]); docID != "" && !seenDocs[docID] {
			seenDocs[docID] = true
			docIDs = append(docIDs, docID)
		}
	}
	existingDocs := map[string]string{}
	if len(docIDs) > 0 {
		if s.documentDAO == nil {
			return nil, fmt.Errorf("document reader is not initialized")
		}
		docs, err := s.documentDAO.GetByIDs(ctx, docIDs)
		if err != nil {
			return nil, fmt.Errorf("document existence lookup: %w", err)
		}
		for _, doc := range docs {
			if doc != nil {
				existingDocs[doc.ID] = doc.KbID
			}
		}
	}
	existingKBs := map[string]bool{}
	if len(summaryKBIDs) > 0 {
		if s.kbDAO == nil {
			return nil, fmt.Errorf("dataset reader is not initialized")
		}
		ids, err := s.kbDAO.GetExistingIDs(ctx, summaryKBIDs)
		if err != nil {
			return nil, fmt.Errorf("dataset existence lookup: %w", err)
		}
		for _, id := range ids {
			existingKBs[id] = true
		}
	}
	filtered := *result
	filtered.IDs = make([]string, 0, len(result.IDs))
	filtered.Chunks = make([]map[string]interface{}, 0, len(result.IDs))
	filtered.Field = make(map[string]map[string]interface{}, len(result.IDs))
	if result.Highlight != nil {
		filtered.Highlight = make(map[string]string)
	}
	for _, id := range result.IDs {
		chunk := result.Field[id]
		kbID := chunkScalar(chunk["kb_id"])
		if chunk == nil || (len(kbIDs) > 0 && !selectedKBs[kbID]) {
			continue
		}
		if isDatasetRaptorChunk(chunk) {
			if !selectedKBs[kbID] || !existingKBs[kbID] {
				continue
			}
		} else if parentKB, ok := existingDocs[chunkScalar(chunk["doc_id"])]; !ok || parentKB != kbID {
			continue
		}
		filtered.IDs = append(filtered.IDs, id)
		filtered.Chunks = append(filtered.Chunks, chunk)
		filtered.Field[id] = chunk
		if text, ok := result.Highlight[id]; ok {
			filtered.Highlight[id] = text
		}
	}
	if removed := len(result.IDs) - len(filtered.IDs); removed > 0 {
		common.Warn("Pruned chunks without a live SQL parent", zap.Int("removed", removed))
	}
	return &filtered, nil
}

// buildIndexNames creates index names for the given tenant IDs
func buildIndexNames(tenantIDs []string) []string {
	indexNames := make([]string, len(tenantIDs))
	for i, tenantID := range tenantIDs {
		indexNames[i] = fmt.Sprintf("multirag_%s", tenantID)
	}
	return indexNames
}
