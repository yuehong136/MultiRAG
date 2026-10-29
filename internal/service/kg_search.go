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
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strings"

	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	modelModule "multirag/internal/entity/models"
)

// KGEntity represents a knowledge graph entity.
type KGEntity struct {
	Name        string  // entity_kwd
	Type        string  // entity_type_kwd
	PageRank    float64 // rank_flt
	Similarity  float64 // _score
	Description string  // content_with_weight
	NhopJSON    string  // n_hop_with_weight
}

// KGRelation represents a relation between two entities.
type KGRelation struct {
	From        string  // from_entity_kwd
	To          string  // to_entity_kwd
	Weight      int     // weight_int
	Description string  // content_with_weight
	Similarity  float64 // engine score
}

// KGCommunityReport represents a community report.
type KGCommunityReport struct {
	Title       string   // docnm_kwd
	Content     string   // content_with_weight
	Weight      float64  // weight_flt
	Entities    string   // entities_kwd
	EntityNames []string // complete keyword array
}

// buildKGDenseExpr computes the query vector and returns a MatchDenseExpr
// for KG hybrid search. Returns nil if embModel or question is empty.
func buildKGDenseExpr(ctx context.Context, embModel *modelModule.EmbeddingModel, question string, topN int) (*types.MatchDenseExpr, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if embModel == nil || question == "" {
		return nil, nil
	}
	if embModel.ModelDriver == nil || embModel.ModelName == nil {
		return nil, fmt.Errorf("KG embedding model is unavailable")
	}
	local := *embModel
	api := modelModule.APIConfig{}
	if local.APIConfig != nil {
		api = *local.APIConfig
	}
	api.Context = ctx
	local.APIConfig = &api
	embCfg := modelModule.EmbeddingConfig{}
	if local.EmbeddingConfig != nil {
		embCfg = *local.EmbeddingConfig
	}
	embCfg.Query = true
	local.EmbeddingConfig = &embCfg
	embeddings, err := local.Embed([]string{question})
	if err != nil {
		return nil, fmt.Errorf("KG entity embed failed: %w", err)
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if len(embeddings) != 1 || len(embeddings[0].Embedding) == 0 {
		return nil, fmt.Errorf("KG embedding returned no vector")
	}
	vector := embeddings[0].Embedding
	return &types.MatchDenseExpr{
		VectorColumnName:  fmt.Sprintf("q_%d_vec", len(vector)),
		EmbeddingData:     vector,
		EmbeddingDataType: "float",
		DistanceType:      "cosine",
		TopN:              topN,
		ExtraOptions:      map[string]interface{}{"similarity": 0.3},
	}, nil
}

// buildHybridExpr returns MatchExprs for hybrid search (dense + text + fusion).
func buildHybridExpr(dense *types.MatchDenseExpr, text *types.MatchTextExpr, topN int) []interface{} {
	return []interface{}{
		dense,
		text,
		&types.FusionExpr{
			Method:       "weighted_sum",
			TopN:         topN,
			FusionParams: map[string]interface{}{"weights": "0.05,0.95"},
		},
	}
}

// buildEntitySearchRequest constructs a SearchRequest for KG entities.
// dense may be nil for text-only search.
func buildEntitySearchRequest(kbIDs []string, question string, dense *types.MatchDenseExpr, topN int) *types.SearchRequest {
	req := &types.SearchRequest{
		KbIDs:        kbIDs,
		SelectFields: []string{"entity_kwd", "entity_type_kwd", "rank_flt", "content_with_weight", "n_hop_with_weight"},
		Limit:        topN,
		Filter:       map[string]interface{}{"knowledge_graph_kwd": "entity"},
	}
	if question == "" {
		return req
	}
	textExpr := &types.MatchTextExpr{
		Fields:       []string{"entity_kwd^10", "content_ltks^2"},
		MatchingText: question,
		TopN:         topN,
	}
	if dense != nil {
		req.MatchExprs = buildHybridExpr(dense, textExpr, topN)
		req.RankFeature = map[string]float64{"pagerank_fea": 10.0}
	} else {
		req.MatchExprs = []interface{}{textExpr}
	}
	return req
}

// buildEntityTypeSearchRequest constructs a SearchRequest for KG entities by type.
func buildEntityTypeSearchRequest(kbIDs []string, typeKeywords []string, topN int) *types.SearchRequest {
	req := &types.SearchRequest{
		KbIDs:        kbIDs,
		SelectFields: []string{"entity_kwd", "entity_type_kwd", "rank_flt", "content_with_weight", "n_hop_with_weight"},
		Limit:        topN,
		Filter: map[string]interface{}{
			"knowledge_graph_kwd": "entity",
		},
	}
	if len(typeKeywords) > 0 {
		filters := make([]interface{}, len(typeKeywords))
		for i, t := range typeKeywords {
			filters[i] = t
		}
		req.Filter["entity_type_kwd"] = filters
	}
	return req
}

// buildRelationSearchRequest constructs a SearchRequest for KG relations.
// dense may be nil for text-only search.
func buildRelationSearchRequest(kbIDs []string, question string, dense *types.MatchDenseExpr, topN int) *types.SearchRequest {
	req := &types.SearchRequest{
		KbIDs:        kbIDs,
		SelectFields: []string{"from_entity_kwd", "to_entity_kwd", "weight_int", "content_with_weight"},
		Limit:        topN,
		Filter:       map[string]interface{}{"knowledge_graph_kwd": "relation"},
	}
	if question != "" {
		textExpr := &types.MatchTextExpr{
			Fields:       []string{"content_ltks"},
			MatchingText: question,
			TopN:         topN,
		}
		if dense != nil {
			req.MatchExprs = buildHybridExpr(dense, textExpr, topN)
		} else {
			req.MatchExprs = []interface{}{textExpr}
		}
	}
	return req
}

// buildCommunitySearchRequest constructs a SearchRequest for KG community reports.
// Matches community reports whose entities_kwd contains any of the given entity names.
func buildCommunitySearchRequest(kbIDs []string, entityNames []string, topN int) *types.SearchRequest {
	req := &types.SearchRequest{
		KbIDs:        kbIDs,
		SelectFields: []string{"docnm_kwd", "content_with_weight", "weight_flt", "entities_kwd"},
		Limit:        topN,
		Filter: map[string]interface{}{
			"knowledge_graph_kwd": "community_report",
		},
		OrderBy: (&types.OrderByExpr{}).Desc("weight_flt"),
	}
	if len(entityNames) > 0 {
		filters := make([]interface{}, len(entityNames))
		for i, name := range entityNames {
			filters[i] = name
		}
		req.Filter["entities_kwd"] = filters
	}
	return req
}

// buildTypeSamplesSearchRequest constructs a SearchRequest for ty2ents data.
func buildTypeSamplesSearchRequest(kbIDs []string) *types.SearchRequest {
	return &types.SearchRequest{
		KbIDs:        kbIDs,
		SelectFields: []string{"content_with_weight"},
		Limit:        10000,
		Filter:       map[string]interface{}{"knowledge_graph_kwd": "ty2ents"},
	}
}

// ParseKGEntityChunks converts raw search result chunks into KGEntity slices.
func ParseKGEntityChunks(chunks []map[string]interface{}) []KGEntity {
	var entities []KGEntity
	for _, chunk := range chunks {
		if !kgFiniteFields(chunk, "rank_flt", "_score", "score") {
			continue
		}
		e := KGEntity{}
		e.Name = kgString(chunk["entity_kwd"])
		if e.Name == "" {
			continue
		}
		e.Type = kgString(chunk["entity_type_kwd"])
		e.Description, _ = chunk["content_with_weight"].(string)
		e.NhopJSON, _ = chunk["n_hop_with_weight"].(string)
		e.PageRank = kgNumber(chunk["rank_flt"])
		e.Similarity = kgScore(chunk)
		entities = append(entities, e)
	}
	return entities
}

// ParseKGRelationChunks converts raw search result chunks into KGRelation slices.
func ParseKGRelationChunks(chunks []map[string]interface{}) []KGRelation {
	var relations []KGRelation
	for _, chunk := range chunks {
		if !kgFiniteFields(chunk, "weight_int", "_score", "score") {
			continue
		}
		r := KGRelation{}
		r.From = kgString(chunk["from_entity_kwd"])
		r.To = kgString(chunk["to_entity_kwd"])
		r.Description, _ = chunk["content_with_weight"].(string)
		weight := kgNumber(chunk["weight_int"])
		if math.Trunc(weight) != weight || weight >= float64(math.MaxInt) || weight < float64(math.MinInt) {
			continue
		}
		r.Weight = int(weight)
		if r.From == "" || r.To == "" {
			continue
		}
		r.Similarity = kgScore(chunk)
		relations = append(relations, r)
	}
	return relations
}

// ParseKGCommunityReportChunks converts raw search result chunks into KGCommunityReport slices.
func ParseKGCommunityReportChunks(chunks []map[string]interface{}) []KGCommunityReport {
	var reports []KGCommunityReport
	for _, chunk := range chunks {
		if !kgFiniteFields(chunk, "weight_flt") {
			continue
		}
		r := KGCommunityReport{}
		r.Title, _ = chunk["docnm_kwd"].(string)
		r.Content, _ = chunk["content_with_weight"].(string)
		r.Entities = kgString(chunk["entities_kwd"])
		r.EntityNames = kgStrings(chunk["entities_kwd"])
		r.Weight = kgNumber(chunk["weight_flt"])
		if r.Title == "" && r.Content == "" {
			continue
		}
		reports = append(reports, r)
	}
	return reports
}

// ParseKGTypeSamplesChunks converts raw search result chunks into a type→entities map.
func ParseKGTypeSamplesChunks(chunks []map[string]interface{}) map[string][]string {
	result := make(map[string][]string)
	for _, chunk := range chunks {
		content, ok := chunk["content_with_weight"].(string)
		if !ok || content == "" {
			continue
		}
		var typeMap map[string][]string
		if err := json.Unmarshal([]byte(content), &typeMap); err != nil {
			continue
		}
		for typ, entities := range typeMap {
			if strings.TrimSpace(typ) == "" || entities == nil {
				continue
			}
			for _, name := range entities {
				if strings.TrimSpace(name) != "" {
					result[typ] = append(result[typ], name)
				}
			}
		}
	}
	return result
}

// NhopEntityNames extracts unique entity names from n_hop_with_weight JSON string.
// The JSON format is: [{"path": ["A", "B", "C"], "weights": [0.8, 0.5]}, ...]
// Returns entity names in order of first appearance, with duplicates removed.
func NhopEntityNames(nHopJSON string) []string {
	type nhopItem struct {
		Path    []string  `json:"path"`
		Weights []float64 `json:"weights"`
	}
	var data []nhopItem
	if err := json.Unmarshal([]byte(nHopJSON), &data); err != nil {
		return nil
	}
	seen := make(map[string]struct{})
	var names []string
	for _, item := range data {
		for _, name := range item.Path {
			if name == "" {
				continue
			}
			if _, ok := seen[name]; !ok {
				seen[name] = struct{}{}
				names = append(names, name)
			}
		}
	}
	return names
}

func kgString(value any) string {
	switch v := value.(type) {
	case string:
		return v
	case []string:
		if len(v) > 0 {
			return v[0]
		}
	case []any:
		if len(v) > 0 {
			s, _ := v[0].(string)
			return s
		}
	}
	return ""
}

func kgNumber(value any) float64 {
	var n float64
	switch v := value.(type) {
	case float64:
		n = v
	case float32:
		n = float64(v)
	case int:
		n = float64(v)
	case int64:
		n = float64(v)
	case json.Number:
		n, _ = v.Float64()
	}
	if math.IsNaN(n) || math.IsInf(n, 0) {
		return 0
	}
	return n
}

func kgStrings(value any) []string {
	var result []string
	switch v := value.(type) {
	case string:
		if v != "" {
			return []string{v}
		}
	case []string:
		return append([]string{}, v...)
	case []any:
		for _, raw := range v {
			if s, ok := raw.(string); ok && s != "" {
				result = append(result, s)
			}
		}
	}
	return result
}

func kgFiniteFields(chunk map[string]any, fields ...string) bool {
	for _, field := range fields {
		v, exists := chunk[field]
		if !exists || v == nil {
			continue
		}
		switch n := v.(type) {
		case float64:
			if math.IsNaN(n) || math.IsInf(n, 0) {
				return false
			}
		case float32:
			if math.IsNaN(float64(n)) || math.IsInf(float64(n), 0) {
				return false
			}
		case json.Number:
			if f, err := n.Float64(); err != nil || math.IsNaN(f) || math.IsInf(f, 0) {
				return false
			}
		case int, int64:
		default:
			return false
		}
	}
	return true
}

func kgScore(chunk map[string]interface{}) float64 {
	if v, ok := chunk["_score"]; ok {
		return kgNumber(v)
	}
	return kgNumber(chunk["score"])
}

// KGSearchService supplies the preparatory KG queries. It does not enable the
// full UseKG retrieval pipeline. Every query resolves readable datasets first.
type KGSearchService struct {
	store    engine.DocEngine
	kbReader interface {
		GetReadableByID(context.Context, string, string) (*entity.Knowledgebase, error)
	}
}

func NewKGSearchService(store engine.DocEngine) *KGSearchService {
	return &KGSearchService{store: store, kbReader: dao.NewKnowledgebaseDAO()}
}

type kgScope struct {
	index string
	kbIDs []string
}

func (s *KGSearchService) scopes(ctx context.Context, userID string, kbIDs []string, topN int) ([]kgScope, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if strings.TrimSpace(userID) == "" {
		return nil, fmt.Errorf("KG user is required")
	}
	if topN < 0 || topN > 10000 {
		return nil, fmt.Errorf("KG limit must be between 0 and 10000")
	}
	if s == nil || s.kbReader == nil {
		return nil, fmt.Errorf("KG dataset resolver is unavailable")
	}
	groups := []kgScope{}
	seen := map[string]bool{}
	for _, id := range kbIDs {
		if strings.TrimSpace(id) == "" {
			return nil, fmt.Errorf("KG dataset ID is empty")
		}
		if seen[id] {
			continue
		}
		seen[id] = true
		kb, err := s.kbReader.GetReadableByID(ctx, id, userID)
		if err != nil {
			return nil, fmt.Errorf("KG dataset access denied: %w", err)
		}
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if kb == nil || kb.ID != id || kb.TenantID == "" {
			return nil, fmt.Errorf("KG dataset scope is invalid")
		}
		for _, c := range kb.TenantID {
			if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '-') {
				return nil, fmt.Errorf("KG tenant scope is invalid")
			}
		}
		index := "multirag_" + kb.TenantID
		// Infinity limits filter-only results before merging physical tables.
		// Query per dataset so our global sort can retain the actual top rows.
		groups = append(groups, kgScope{index: index, kbIDs: []string{id}})
	}
	return groups, nil
}

func (s *KGSearchService) search(ctx context.Context, scopes []kgScope, builder func([]string) *types.SearchRequest) ([]map[string]interface{}, error) {
	if len(scopes) == 0 {
		return []map[string]interface{}{}, nil
	}
	if s.store == nil {
		return nil, fmt.Errorf("KG engine is unavailable")
	}
	chunks := []map[string]interface{}{}
	for _, scope := range scopes {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		req := builder(append([]string{}, scope.kbIDs...))
		req.IndexNames = []string{scope.index}
		result, err := s.store.Search(ctx, req)
		if err != nil {
			return nil, fmt.Errorf("KG search failed: %w", err)
		}
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if result == nil {
			return nil, fmt.Errorf("KG engine returned no result")
		}
		chunks = append(chunks, result.Chunks...)
	}
	return chunks, nil
}

// SearchKGEntities authorizes all datasets before optional query embedding or IO.
func (s *KGSearchService) SearchKGEntities(ctx context.Context, userID string, kbIDs []string, question string, embModel *modelModule.EmbeddingModel, topN int) ([]KGEntity, error) {
	scopes, err := s.scopes(ctx, userID, kbIDs, topN)
	if err != nil {
		return nil, err
	}
	if len(scopes) == 0 || topN == 0 || strings.TrimSpace(question) == "" {
		return []KGEntity{}, nil
	}
	dense, err := buildKGDenseExpr(ctx, embModel, question, topN)
	if err != nil {
		return nil, err
	}
	chunks, err := s.search(ctx, scopes, func(ids []string) *types.SearchRequest { return buildEntitySearchRequest(ids, question, dense, topN) })
	if err != nil {
		return nil, err
	}
	return trimKGEntities(ParseKGEntityChunks(chunks), topN), nil
}

func (s *KGSearchService) SearchKGEntitiesByTypes(ctx context.Context, userID string, kbIDs []string, keywords []string, topN int) ([]KGEntity, error) {
	scopes, err := s.scopes(ctx, userID, kbIDs, topN)
	if err != nil {
		return nil, err
	}
	if len(scopes) == 0 || topN == 0 || len(keywords) == 0 {
		return []KGEntity{}, nil
	}
	for _, word := range keywords {
		if strings.TrimSpace(word) == "" {
			return nil, fmt.Errorf("KG type keyword is empty")
		}
	}
	chunks, err := s.search(ctx, scopes, func(ids []string) *types.SearchRequest {
		req := buildEntityTypeSearchRequest(ids, keywords, topN)
		req.OrderBy = (&types.OrderByExpr{}).Desc("rank_flt")
		return req
	})
	if err != nil {
		return nil, err
	}
	return trimKGEntities(ParseKGEntityChunks(chunks), topN), nil
}

func trimKGEntities(rows []KGEntity, topN int) []KGEntity {
	sort.SliceStable(rows, func(i, j int) bool {
		if rows[i].Similarity != rows[j].Similarity {
			return rows[i].Similarity > rows[j].Similarity
		}
		if rows[i].PageRank != rows[j].PageRank {
			return rows[i].PageRank > rows[j].PageRank
		}
		return rows[i].Name < rows[j].Name
	})
	if len(rows) > topN {
		rows = rows[:topN]
	}
	return rows
}

func (s *KGSearchService) SearchKGRelations(ctx context.Context, userID string, kbIDs []string, question string, embModel *modelModule.EmbeddingModel, topN int) ([]KGRelation, error) {
	scopes, err := s.scopes(ctx, userID, kbIDs, topN)
	if err != nil {
		return nil, err
	}
	if len(scopes) == 0 || topN == 0 || strings.TrimSpace(question) == "" {
		return []KGRelation{}, nil
	}
	dense, err := buildKGDenseExpr(ctx, embModel, question, topN)
	if err != nil {
		return nil, err
	}
	chunks, err := s.search(ctx, scopes, func(ids []string) *types.SearchRequest { return buildRelationSearchRequest(ids, question, dense, topN) })
	if err != nil {
		return nil, err
	}
	rows := ParseKGRelationChunks(chunks)
	sort.SliceStable(rows, func(i, j int) bool {
		if rows[i].Similarity != rows[j].Similarity {
			return rows[i].Similarity > rows[j].Similarity
		}
		if rows[i].From != rows[j].From {
			return rows[i].From < rows[j].From
		}
		return rows[i].To < rows[j].To
	})
	if len(rows) > topN {
		rows = rows[:topN]
	}
	return rows, nil
}

func (s *KGSearchService) SearchKGCommunityReports(ctx context.Context, userID string, kbIDs, names []string, topN int) ([]KGCommunityReport, error) {
	scopes, err := s.scopes(ctx, userID, kbIDs, topN)
	if err != nil {
		return nil, err
	}
	if len(scopes) == 0 || topN == 0 || len(names) == 0 {
		return []KGCommunityReport{}, nil
	}
	for _, name := range names {
		if strings.TrimSpace(name) == "" {
			return nil, fmt.Errorf("KG entity name is empty")
		}
	}
	chunks, err := s.search(ctx, scopes, func(ids []string) *types.SearchRequest { return buildCommunitySearchRequest(ids, names, topN) })
	if err != nil {
		return nil, err
	}
	rows := ParseKGCommunityReportChunks(chunks)
	sort.SliceStable(rows, func(i, j int) bool {
		if rows[i].Weight != rows[j].Weight {
			return rows[i].Weight > rows[j].Weight
		}
		return rows[i].Title < rows[j].Title
	})
	if len(rows) > topN {
		rows = rows[:topN]
	}
	return rows, nil
}

func (s *KGSearchService) SearchKGTypeSamples(ctx context.Context, userID string, kbIDs []string) (map[string][]string, error) {
	scopes, err := s.scopes(ctx, userID, kbIDs, 10000)
	if err != nil {
		return nil, err
	}
	chunks, err := s.search(ctx, scopes, buildTypeSamplesSearchRequest)
	if err != nil {
		return nil, err
	}
	return ParseKGTypeSamplesChunks(chunks), nil
}
