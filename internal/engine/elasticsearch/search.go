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

package elasticsearch

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"sort"
	"strconv"
	"strings"

	"github.com/elastic/go-elasticsearch/v8/esapi"
	"go.uber.org/zap"

	"multirag/internal/common"
	"multirag/internal/engine/types"
)

// SearchResponse Elasticsearch search response
type SearchResponse struct {
	Hits struct {
		Total struct {
			Value int64 `json:"value"`
		} `json:"total"`
		Hits []struct {
			ID        string                 `json:"_id"`
			Score     float64                `json:"_score"`
			Highlight map[string]interface{} `json:"highlight"`
			Source    map[string]interface{} `json:"_source"`
		} `json:"hits"`
	} `json:"hits"`
	Aggregations map[string]interface{} `json:"aggregations"`
}

// Search executes search with unified types.SearchRequest
func (e *elasticsearchEngine) Search(ctx context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	return e.searchUnified(ctx, req)
}

// searchUnified handles the unified types.SearchRequest
func (e *elasticsearchEngine) searchUnified(ctx context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	if req == nil || len(req.IndexNames) == 0 {
		return nil, fmt.Errorf("index names cannot be empty")
	}

	if req.KbIDs != nil && len(req.KbIDs) == 0 {
		return &types.SearchResult{Chunks: []map[string]interface{}{}}, nil
	}
	for _, index := range req.IndexNames {
		if err := validateIndex(index); err != nil {
			return nil, err
		}
	}
	// Build pagination parameters
	offset := req.Offset
	limit := req.Limit
	if limit <= 0 {
		limit = 30 // default ES size
	}

	// Build filter clauses (default: available=1, meaning available_int >= 1)
	// Reference: rag/utils/es_conn.py L60-L78
	filterClauses := buildFilterClauses(req.KbIDs, 1)
	if _, explicit := req.Filter["available_int"]; explicit || req.IncludeUnavailable {
		filterClauses = nil
		if len(req.KbIDs) > 0 {
			filterClauses = append(filterClauses, map[string]interface{}{"terms": map[string]interface{}{"kb_id": req.KbIDs}})
		}
	}
	if len(req.Filter) > 0 {
		filterQuery, err := conditionQuery(req.Filter)
		if err != nil {
			return nil, err
		}
		filterClauses = append(filterClauses, filterQuery)
	}

	// Build search query body
	queryBody := make(map[string]interface{})

	// Determine search type from MatchExprs
	var matchText string
	var matchFields []string
	var matchDense interface{}
	var textWeight float64 = 1.0
	var vectorWeight float64 = 1.0
	var hasVectorMatch bool
	var fusion *types.FusionExpr

	for _, expr := range req.MatchExprs {
		if expr == nil {
			continue
		}
		switch e := expr.(type) {
		case string:
			matchText = e
		case *types.MatchTextExpr:
			if e == nil {
				continue
			}
			matchText = e.MatchingText
			matchFields = e.Fields
		case *types.MatchDenseExpr:
			if e == nil {
				continue
			}
			hasVectorMatch = true
			matchDense = e
			textWeight = 0.3 // default, should be passed via SimilarityThreshold
		case *types.FusionExpr:
			if e != nil {
				if fusion != nil {
					return nil, fmt.Errorf("multiple fusion expressions are unsupported")
				}
				fusion = e
			}
		}
	}
	if fusion != nil {
		if !hasVectorMatch || matchText == "" {
			return nil, fmt.Errorf("weighted fusion requires text and dense expressions")
		}
		var err error
		textWeight, vectorWeight, err = esFusionWeights(fusion)
		if err != nil {
			return nil, err
		}
	}
	rankFeatures, err := esRankFeatures(req.RankFeature)
	if err != nil {
		return nil, err
	}
	orders, err := esSortFields(req.OrderBy)
	if err != nil {
		return nil, err
	}

	var vectorFieldName string
	if !hasVectorMatch {
		// Keyword-only search
		queryBody["query"] = buildESKeywordQuery(matchText, filterClauses, 1.0, matchFields)
	} else {
		// Hybrid search: keyword + vector
		// Calculate text weight (use SimilarityThreshold as text weight if provided)

		// Build boolean query for text match and filters
		boolQuery := buildESKeywordQuery(matchText, filterClauses, 1.0, matchFields)
		// Add boost to the bool query (as in Python code)
		if boolMap, ok := boolQuery["bool"].(map[string]interface{}); ok {
			boolMap["boost"] = textWeight
		}
		// Build kNN query
		var vectorData []float64
		if md, ok := matchDense.(*types.MatchDenseExpr); ok {
			vectorData = md.EmbeddingData
			vectorFieldName = md.VectorColumnName
			k := md.TopN
			if k <= 0 {
				k = req.Limit
			}
			if k <= 0 {
				k = 1024
			}
			numCandidates := k * 2

			knnQuery := map[string]interface{}{
				"field":          vectorFieldName,
				"query_vector":   vectorData,
				"k":              k,
				"num_candidates": numCandidates,
				"filter":         boolQuery,
				"similarity":     0.0,
			}
			if value, present := md.ExtraOptions["similarity"]; present {
				similarity, ok := value.(float64)
				if !ok || math.IsNaN(similarity) || math.IsInf(similarity, 0) {
					return nil, fmt.Errorf("dense similarity must be a finite number")
				}
				knnQuery["similarity"] = similarity
			}
			if fusion != nil {
				// Explicit fusion follows the Python scalar-only kNN filter:
				// vectors may match without satisfying the lexical branch.
				// Keep the legacy lexical prefilter for callers without FusionExpr.
				knnQuery["filter"] = map[string]interface{}{"bool": map[string]interface{}{"filter": filterClauses}}
				knnQuery["boost"] = vectorWeight
			}

			queryBody["knn"] = knnQuery
			queryBody["query"] = boolQuery
		}

		// Add vector column to Source fields (matching Python ES: src.append(f"q_{len(q_vec)}_vec"))
		// Only modify Source if it was explicitly set by the caller
		if vectorFieldName != "" && len(req.SelectFields) > 0 {
			sourceFields := append([]string(nil), req.SelectFields...)
			// Check if vector column already in source
			found := false
			for _, f := range sourceFields {
				if f == vectorFieldName {
					found = true
					break
				}
			}
			if !found {
				sourceFields = append(sourceFields, vectorFieldName)
			}
			queryBody["_source"] = sourceFields
		}
	}
	if len(rankFeatures) > 0 {
		if query, ok := queryBody["query"].(map[string]interface{}); ok {
			if boolQuery, ok := query["bool"].(map[string]interface{}); ok {
				boolQuery["should"] = rankFeatures
			}
		}
	}
	if len(orders) > 0 {
		queryBody["sort"] = orders
	}

	if len(req.SelectFields) > 0 {
		if _, ok := queryBody["_source"]; !ok {
			queryBody["_source"] = req.SelectFields
		}
	}
	queryBody["size"] = limit
	queryBody["from"] = offset

	// Serialize query
	var buf bytes.Buffer
	if err := json.NewEncoder(&buf).Encode(queryBody); err != nil {
		return nil, fmt.Errorf("error encoding query: %w", err)
	}

	// Log search details
	common.Debug("Elasticsearch searching indices", zap.Strings("indices", req.IndexNames))
	common.Debug("Elasticsearch DSL", zap.Any("dsl", queryBody))

	// Build search request
	reqES := esapi.SearchRequest{
		Index: req.IndexNames,
		Body:  &buf,
	}

	// Execute search
	res, err := reqES.Do(ctx, e.client)
	if err != nil {
		return nil, fmt.Errorf("search failed: %w", err)
	}
	defer res.Body.Close()

	if res.IsError() {
		bodyBytes, err := io.ReadAll(res.Body)
		if err != nil {
			common.Error("Elasticsearch failed to read error response body", err)
		} else {
			common.Warn("Elasticsearch error response", zap.String("body", string(bodyBytes)))
		}
		return nil, fmt.Errorf("Elasticsearch returned error: %s", res.Status())
	}

	// Parse response
	var esResp SearchResponse
	decoder := json.NewDecoder(res.Body)
	metadataOnly := len(req.IndexNames) > 0
	for _, index := range req.IndexNames {
		if !strings.HasPrefix(index, "multirag_doc_meta_") {
			metadataOnly = false
			break
		}
	}
	if metadataOnly {
		// Metadata values may contain integers beyond float64 precision.
		// Chunk consumers retain their existing numeric representation.
		decoder.UseNumber()
	}
	if err := decoder.Decode(&esResp); err != nil {
		return nil, fmt.Errorf("error parsing response: %w", err)
	}

	// Convert to unified response
	chunks := convertESResponse(&esResp, vectorFieldName)
	return &types.SearchResult{
		Chunks: chunks,
		Total:  esResp.Hits.Total.Value,
	}, nil
}

// calculatePagination calculates offset and limit based on page, size and topK
func calculatePagination(page, size, topK int) (int, int) {
	if page < 1 {
		page = 1
	}
	if size <= 0 {
		size = 30
	}
	if topK <= 0 {
		topK = 1024
	}

	RERANK_LIMIT := max(30, (64/size)*size)
	if RERANK_LIMIT < size {
		RERANK_LIMIT = size
	}
	if RERANK_LIMIT > topK {
		RERANK_LIMIT = topK
	}

	offset := (page - 1) * RERANK_LIMIT
	if offset < 0 {
		offset = 0
	}

	return offset, RERANK_LIMIT
}

// buildFilterClauses builds ES filter clauses from kb_ids, doc_ids and available_int
// Reference: rag/utils/es_conn.py L60-L78
// When available=0: available_int < 1
// When available!=0: NOT (available_int < 1)
func buildFilterClauses(kbIDs []string, available int) []map[string]interface{} {
	var filters []map[string]interface{}

	if len(kbIDs) > 0 {
		filters = append(filters, map[string]interface{}{
			"terms": map[string]interface{}{"kb_id": kbIDs},
		})
	}

	// Add available_int filter
	// Reference: rag/utils/es_conn.py L63-L68
	if available == 0 {
		// available_int < 1
		filters = append(filters, map[string]interface{}{
			"range": map[string]interface{}{
				"available_int": map[string]interface{}{
					"lt": 1,
				},
			},
		})
	} else {
		// must_not: available_int < 1 (i.e., available_int >= 1)
		filters = append(filters, map[string]interface{}{
			"bool": map[string]interface{}{
				"must_not": []map[string]interface{}{
					{
						"range": map[string]interface{}{
							"available_int": map[string]interface{}{
								"lt": 1,
							},
						},
					},
				},
			},
		})
	}

	return filters
}

// buildESKeywordQuery builds keyword-only search query for ES
// Uses query_string if matchText is in query_string format, otherwise uses multi_match
// boost is applied to the text match clause (query_string or multi_match)
func buildESKeywordQuery(matchText string, filterClauses []map[string]interface{}, boost float64, selectedFields ...[]string) map[string]interface{} {
	if matchText == "" {
		return map[string]interface{}{"bool": map[string]interface{}{"filter": filterClauses}}
	}
	var mustClause map[string]interface{}

	fields := []string{"title_tks^10", "title_sm_tks^5", "important_kwd^30", "important_tks^20", "question_tks^20", "content_ltks^2", "content_sm_ltks"}
	if len(selectedFields) > 0 && len(selectedFields[0]) > 0 {
		fields = append([]string(nil), selectedFields[0]...)
	}
	// Use query_string for complex queries
	queryString := map[string]interface{}{
		"query":                matchText,
		"fields":               fields,
		"type":                 "best_fields",
		"minimum_should_match": "30%",
		"boost":                boost,
	}
	mustClause = map[string]interface{}{
		"query_string": queryString,
	}

	return map[string]interface{}{
		"bool": map[string]interface{}{
			"must":   mustClause,
			"filter": filterClauses,
		},
	}
}

// esFusionWeights uses the text,dense order of the public fusion contract,
// independent of the order of those expressions in MatchExprs.
func esFusionWeights(fusion *types.FusionExpr) (float64, float64, error) {
	if fusion.Method != "weighted_sum" {
		return 0, 0, fmt.Errorf("unsupported Elasticsearch fusion method %q", fusion.Method)
	}
	value, ok := fusion.FusionParams["weights"].(string)
	parts := strings.Split(value, ",")
	if !ok || len(parts) != 2 {
		return 0, 0, fmt.Errorf("fusion weights must contain text and dense weights")
	}
	weights := [2]float64{}
	for i, part := range parts {
		weight, err := strconv.ParseFloat(strings.TrimSpace(part), 64)
		if err != nil || math.IsNaN(weight) || math.IsInf(weight, 0) || weight < 0 {
			return 0, 0, fmt.Errorf("fusion weights must be finite and non-negative")
		}
		weights[i] = weight
	}
	if weights[0] == 0 && weights[1] == 0 {
		return 0, 0, fmt.Errorf("fusion weights cannot both be zero")
	}
	return weights[0], weights[1], nil
}

func esRankFeatures(features map[string]float64) ([]map[string]interface{}, error) {
	keys := make([]string, 0, len(features))
	for key := range features {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	clauses := make([]map[string]interface{}, 0, len(keys))
	for _, key := range keys {
		boost := features[key]
		if strings.TrimSpace(key) == "" || math.IsNaN(boost) || math.IsInf(boost, 0) || boost < 0 {
			return nil, fmt.Errorf("rank features require non-empty names and finite non-negative boosts")
		}
		field := key
		if field != common.PAGERANK_FLD {
			field = common.TAG_FLD + "." + key
		}
		clauses = append(clauses, map[string]interface{}{"rank_feature": map[string]interface{}{"field": field, "linear": map[string]interface{}{}, "boost": boost}})
	}
	return clauses, nil
}

func esSortFields(orderBy *types.OrderByExpr) ([]map[string]interface{}, error) {
	if orderBy == nil {
		return nil, nil
	}
	orders := make([]map[string]interface{}, 0, len(orderBy.Fields))
	for _, field := range orderBy.Fields {
		if !types.ValidFieldName(field.Field) || (field.Type != types.SortAsc && field.Type != types.SortDesc) {
			return nil, fmt.Errorf("invalid Elasticsearch sort field or direction")
		}
		// The stored logical id is a text field, matching Python's exclusion.
		if field.Field == "id" {
			continue
		}
		direction := "asc"
		if field.Type == types.SortDesc {
			direction = "desc"
		}
		info := map[string]interface{}{"order": direction, "unmapped_type": "keyword"}
		if field.Field == "page_num_int" || field.Field == "top_int" {
			info["unmapped_type"], info["mode"], info["numeric_type"] = "float", "avg", "double"
		} else if strings.HasSuffix(field.Field, "_int") || strings.HasSuffix(field.Field, "_flt") {
			info["unmapped_type"] = "float"
		}
		orders = append(orders, map[string]interface{}{field.Field: info})
	}
	return orders, nil
}

// convertESResponse converts ES SearchResponse to unified chunks format
func convertESResponse(esResp *SearchResponse, vectorFieldName string) []map[string]interface{} {
	if esResp == nil || esResp.Hits.Hits == nil {
		return []map[string]interface{}{}
	}

	chunks := make([]map[string]interface{}, len(esResp.Hits.Hits))
	for i, hit := range esResp.Hits.Hits {

		//// vectorField is list of float64, which need to be converted to float32

		chunks[i] = hit.Source
		if chunks[i] == nil {
			chunks[i] = map[string]interface{}{}
		}
		chunks[i]["_score"] = hit.Score
		chunks[i]["_id"] = hit.ID
		if len(hit.Highlight) > 0 {
			chunks[i]["highlight"] = hit.Highlight
		}
		//vectorField := hit.Source[vectorFieldName]
		//chunks[i][vectorFieldName] = utility.Float64ToFloat32(vectorField)
	}
	return chunks
}

// Helper query builder functions (legacy)

// BuildMatchTextQuery builds a text match query
func BuildMatchTextQuery(fields []string, text string, fuzziness string) map[string]interface{} {
	query := map[string]interface{}{
		"multi_match": map[string]interface{}{
			"query":  text,
			"fields": fields,
		},
	}

	if fuzziness != "" {
		if multiMatch, ok := query["multi_match"].(map[string]interface{}); ok {
			multiMatch["fuzziness"] = fuzziness
		}
	}

	return query
}

// BuildTermQuery builds a term query
func BuildTermQuery(field string, value interface{}) map[string]interface{} {
	return map[string]interface{}{
		"term": map[string]interface{}{
			field: value,
		},
	}
}

// BuildRangeQuery builds a range query
func BuildRangeQuery(field string, from, to interface{}) map[string]interface{} {
	rangeQuery := make(map[string]interface{})
	if from != nil {
		rangeQuery["gte"] = from
	}
	if to != nil {
		rangeQuery["lte"] = to
	}

	return map[string]interface{}{
		"range": map[string]interface{}{
			field: rangeQuery,
		},
	}
}

// BuildBoolQuery builds a bool query
func BuildBoolQuery() map[string]interface{} {
	return map[string]interface{}{
		"bool": make(map[string]interface{}),
	}
}

// AddMust adds must clause to bool query
func AddMust(query map[string]interface{}, clauses ...map[string]interface{}) {
	if boolQuery, ok := query["bool"].(map[string]interface{}); ok {
		if _, exists := boolQuery["must"]; !exists {
			boolQuery["must"] = []map[string]interface{}{}
		}
		if must, ok := boolQuery["must"].([]map[string]interface{}); ok {
			boolQuery["must"] = append(must, clauses...)
		}
	}
}

// GetFields selects stored ES fields without mutating the result or erasing
// indexed keyword arrays when no Infinity-style source alias is present.
func (e *elasticsearchEngine) GetFields(chunks []map[string]interface{}, fields []string) map[string]map[string]interface{} {
	selected := map[string]bool{}
	for _, field := range fields {
		selected[field] = true
	}
	result := map[string]map[string]interface{}{}
	for _, chunk := range chunks {
		id := chunkID(chunk)
		if id == "" {
			continue
		}
		row := map[string]interface{}{}
		for key, value := range chunk {
			row[key] = value
		}
		for source, targets := range map[string][]string{"docnm": {"docnm_kwd", "title_tks", "title_sm_tks"}, "content": {"content_with_weight", "content_ltks", "content_sm_ltks"}, "authors": {"authors_tks", "authors_sm_tks"}} {
			if value, ok := chunk[source]; ok {
				for _, target := range targets {
					if _, exists := row[target]; !exists {
						row[target] = value
					}
				}
			}
		}
		output := map[string]interface{}{}
		for key, value := range row {
			if len(fields) == 0 || selected[key] {
				output[key] = value
			}
		}
		result[id] = output
	}
	return result
}

// AddShould adds should clause to bool query
func AddShould(query map[string]interface{}, clauses ...map[string]interface{}) {
	if boolQuery, ok := query["bool"].(map[string]interface{}); ok {
		if _, exists := boolQuery["should"]; !exists {
			boolQuery["should"] = []map[string]interface{}{}
		}
		if should, ok := boolQuery["should"].([]map[string]interface{}); ok {
			boolQuery["should"] = append(should, clauses...)
		}
	}
}

// AddFilter adds filter clause to bool query
func AddFilter(query map[string]interface{}, clauses ...map[string]interface{}) {
	if boolQuery, ok := query["bool"].(map[string]interface{}); ok {
		if _, exists := boolQuery["filter"]; !exists {
			boolQuery["filter"] = []map[string]interface{}{}
		}
		if filter, ok := boolQuery["filter"].([]map[string]interface{}); ok {
			boolQuery["filter"] = append(filter, clauses...)
		}
	}
}

// AddMustNot adds must_not clause to bool query
func AddMustNot(query map[string]interface{}, clauses ...map[string]interface{}) {
	if boolQuery, ok := query["bool"].(map[string]interface{}); ok {
		if _, exists := boolQuery["must_not"]; !exists {
			boolQuery["must_not"] = []map[string]interface{}{}
		}
		if mustNot, ok := boolQuery["must_not"].([]map[string]interface{}); ok {
			boolQuery["must_not"] = append(mustNot, clauses...)
		}
	}
}
