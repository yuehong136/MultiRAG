package kg

import (
	"context"
	"fmt"
	"math"
	"strings"
	"sync"

	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity/models"
)

// Result is a KB-level graph summary, without a source document identity.
// Its score is synthetic; no vector or term similarity is claimed.
type Result struct {
	Content string
	KBID    string
}

// Pipeline shares only immutable engine/resolver dependencies. Models, options,
// query state and cancellation belong to each Retrieval invocation.
type Pipeline struct{ search *KGSearchService }

type config struct {
	similarity                                                    float64
	denseTopK, entityTopN, relationTopN, communityTopN, maxTokens int
}

// Option sets a request-local pipeline limit.
type Option func(*config)

func WithSimThreshold(value float64) Option { return func(c *config) { c.similarity = value } }
func WithDenseTopK(value int) Option        { return func(c *config) { c.denseTopK = value } }
func WithMaxTokens(value int) Option        { return func(c *config) { c.maxTokens = value } }

func NewPipeline(store engine.DocEngine, readers ...DatasetReader) *Pipeline {
	return &Pipeline{search: NewKGSearchService(store, readers...)}
}

// Retrieval resolves exactly one readable KB before model, rewrite or engine IO.
// A document selection must be handled by the caller; this result is a KB-level
// aggregate, never an assertion that graph rows match ordinary document IDs.
func (p *Pipeline) Retrieval(ctx context.Context, userID string, kbIDs []string, question string, chatModel *models.ChatModel, embModel *models.EmbeddingModel, options ...Option) (*Result, error) {
	if p == nil || p.search == nil {
		return nil, fmt.Errorf("KG pipeline unavailable")
	}
	cfg := config{similarity: 0.3, denseTopK: 1024, entityTopN: 6, relationTopN: 6, communityTopN: 1, maxTokens: 8196}
	for _, option := range options {
		if option != nil {
			option(&cfg)
		}
	}
	if math.IsNaN(cfg.similarity) || math.IsInf(cfg.similarity, 0) || cfg.similarity < 0 || cfg.similarity > 1 || cfg.denseTopK < 1 || cfg.denseTopK > 10000 || cfg.maxTokens < 0 {
		return nil, fmt.Errorf("invalid KG pipeline limits")
	}
	if len(kbIDs) != 1 {
		return nil, fmt.Errorf("KG pipeline requires one dataset")
	}
	scopes, err := p.search.scopes(ctx, userID, kbIDs, 50)
	if err != nil {
		return nil, err
	}
	result := &Result{KBID: kbIDs[0]}
	if strings.TrimSpace(question) == "" || cfg.maxTokens == 0 {
		return result, nil
	}
	var pool map[string][]string
	if chatModel != nil {
		rows, err := p.search.search(ctx, scopes, buildTypeSamplesSearchRequest)
		if err != nil {
			return nil, err
		}
		pool = ParseKGTypeSamplesChunks(rows)
	}
	keywords, terms, err := rewrite(ctx, chatModel, question, pool)
	if err != nil {
		return nil, err
	}
	// Explicit empty rewrite arrays do not turn into unqualified entity scans.
	if len(keywords) == 0 && len(terms) == 0 {
		return result, nil
	}
	// Entity and relation branches use the same rewritten query vector. Bind
	// and embed once, then hand each request its own expression/config copy.
	termsText := strings.Join(terms, " ")
	var dense *types.MatchDenseExpr
	if termsText != "" {
		dense, err = buildKGDenseExpr(ctx, embModel, termsText, cfg.denseTopK)
		if err != nil {
			return nil, err
		}
	}
	if dense != nil {
		dense.ExtraOptions["similarity"] = cfg.similarity
	}
	child, cancel := context.WithCancel(ctx)
	defer cancel()
	var entityRows, typeRows, relationRows []map[string]interface{}
	var errs [3]error
	var wg sync.WaitGroup
	run := func(i int, rows *[]map[string]interface{}, f func() ([]map[string]interface{}, error)) {
		defer wg.Done()
		*rows, errs[i] = f()
		if errs[i] != nil {
			cancel()
		}
	}
	wg.Add(3)
	go run(0, &entityRows, func() ([]map[string]interface{}, error) {
		return p.query(child, scopes, dense, termsText, false)
	})
	go run(1, &typeRows, func() ([]map[string]interface{}, error) {
		if len(keywords) == 0 {
			return nil, nil
		}
		return p.search.search(child, scopes, func(ids []string) *types.SearchRequest {
			req := buildEntityTypeSearchRequest(ids, keywords, 10000)
			req.OrderBy = (&types.OrderByExpr{}).Desc("rank_flt")
			return req
		})
	})
	go run(2, &relationRows, func() ([]map[string]interface{}, error) {
		return p.query(child, scopes, dense, termsText, true)
	})
	wg.Wait() // Drain every branch, including failure and parent cancellation.
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	for _, branchErr := range errs {
		if branchErr != nil && !isCancellation(branchErr) {
			return nil, branchErr
		}
	}
	for _, branchErr := range errs {
		if branchErr != nil {
			return nil, branchErr
		}
	}
	entities := make(map[string]*KGEntity)
	for _, row := range ParseKGEntityChunks(entityRows) {
		if row.Similarity < cfg.similarity {
			continue
		}
		row.Sim = row.Similarity
		row.NhopEnts = parseNeighbors(row.NhopJSON)
		if old := entities[row.Name]; old == nil || row.Sim*row.PageRank > old.Sim*old.PageRank {
			copy := row
			entities[row.Name] = &copy
		}
	}
	typeHits := map[string]struct{}{}
	for _, row := range ParseKGEntityChunks(typeRows) {
		typeHits[row.Name] = struct{}{}
	}
	relations := make(map[Edge]*KGRelation)
	for _, row := range ParseKGRelationChunks(relationRows) {
		if row.Similarity < cfg.similarity {
			continue
		}
		row.Sim, row.PageRank = row.Similarity, float64(row.Weight)
		edge := canonicalEdge(Edge{row.From, row.To})
		if old := relations[edge]; old == nil || row.Sim*row.PageRank > old.Sim*old.PageRank {
			copy := row
			relations[edge] = &copy
		}
	}
	paths := canonicalPaths(AnalyzeNHopPaths(entities))
	entities = DoubleHitBoost(entities, typeHits)
	relations = FuseRelationScores(relations, typeHits, paths)
	rankedEntities := SortAndTrimEntities(entities, cfg.entityTopN)
	rankedRelations := SortAndTrimRelations(relations, cfg.relationTopN)
	result.Content = BuildKGContent(rankedEntities, rankedRelations, cfg.maxTokens)
	remaining := cfg.maxTokens - NumTokensFromString(result.Content)
	if remaining > 0 && len(rankedEntities) > 0 {
		names := make([]string, len(rankedEntities))
		for i, row := range rankedEntities {
			names[i] = row.Entity
		}
		rows, err := p.search.search(ctx, scopes, func(ids []string) *types.SearchRequest {
			return buildCommunitySearchRequest(ids, names, cfg.communityTopN)
		})
		if err != nil {
			return nil, err
		}
		result.Content += communityContent(ParseKGCommunityReportChunks(rows), remaining)
	}
	return result, nil
}
