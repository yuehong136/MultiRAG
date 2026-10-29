package kg

import (
	"context"
	"errors"
	"fmt"
	"math"
	"net/http"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity/models"
	"multirag/internal/testsupport"
)

type pipelineStore struct {
	engine.DocEngine
	mu       sync.Mutex
	requests []*types.SearchRequest
	failure  string
	entered  chan struct{}
	drained  chan struct{}
}

func (s *pipelineStore) Search(ctx context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	s.mu.Lock()
	s.requests = append(s.requests, req)
	s.mu.Unlock()
	if !reflect.DeepEqual(req.IndexNames, []string{"multirag_a"}) || !reflect.DeepEqual(req.KbIDs, []string{"kb-a"}) || req.Kind != types.SearchKindKnowledgeGraph || req.Filter["available_int"] != 0 || req.IncludeUnavailable {
		return nil, fmt.Errorf("unscoped graph request")
	}
	kind := req.Filter["knowledge_graph_kwd"]
	if s.entered != nil && kind == "entity" {
		close(s.entered)
		<-ctx.Done()
		close(s.drained)
		return nil, ctx.Err()
	}
	if kind == s.failure {
		if s.entered != nil {
			<-s.entered
		}
		return nil, errors.New("owned graph failure")
	}
	switch kind {
	case "ty2ents":
		return &types.SearchResult{}, nil // No fabricated type producer.
	case "entity":
		if req.Filter["entity_type_kwd"] != nil {
			return &types.SearchResult{Chunks: []map[string]interface{}{{"entity_kwd": "A"}}}, nil
		}
		return &types.SearchResult{Chunks: []map[string]interface{}{{"entity_kwd": []string{"A"}, "rank_flt": 0.5, "_score": 0.8, "content_with_weight": `{"description":"entity A"}`, "n_hop_with_weight": `[{"path":["A","B","C"],"weights":[2,3]}]`}}}, nil
	case "relation":
		textFound := false
		for _, expr := range req.MatchExprs {
			if text, ok := expr.(*types.MatchTextExpr); ok {
				textFound = reflect.DeepEqual(text.Fields, []string{"content_ltks", "from_entity_kwd", "to_entity_kwd"})
			}
		}
		if !textFound {
			return nil, fmt.Errorf("full pipeline relation endpoint fields missing")
		}
		return &types.SearchResult{Chunks: []map[string]interface{}{{"from_entity_kwd": "A", "to_entity_kwd": "B", "weight_int": 2, "_score": 0.5, "content_with_weight": "relation AB"}}}, nil
	case "community_report":
		return &types.SearchResult{Chunks: []map[string]interface{}{{"docnm_kwd": "community", "content_with_weight": `{"report":"community report","evidences":"evidence"}`, "weight_flt": 1}}}, nil
	default:
		return nil, fmt.Errorf("unexpected kind %v", kind)
	}
}

type rewriteDriver struct {
	models.ModelDriver
	answer  string
	err     error
	context context.Context
	calls   int
}

func (d *rewriteDriver) ChatWithMessages(_ string, api *models.APIConfig, _ []models.Message, _ *models.ChatConfig) (*models.ChatResponse, error) {
	d.calls++
	d.context = api.Context
	return &models.ChatResponse{Answer: &d.answer}, d.err
}

func TestPipelineScoringRewriteCommunityAndBudget(t *testing.T) {
	store := &pipelineStore{}
	driver := &rewriteDriver{answer: `{"answer_type_keywords":["TYPE"],"entities_from_query":["A"]}`}
	name := "chat"
	api := &models.APIConfig{Context: context.Background()}
	chat := models.NewChatModel(driver, &name, api)
	embedDriver := &kgEmbedDriver{}
	embedName := "embed"
	embedding := models.NewEmbeddingModel(embedDriver, &embedName, &models.APIConfig{})
	result, err := NewPipeline(store, &kgReader{}).Retrieval(t.Context(), "owner", []string{"kb-a"}, "where A", chat, embedding)
	if err != nil {
		t.Fatal(err)
	}
	// N-hop analyzed before doubling A; existing AB: .5*(2+.4)*2=2.4;
	// new BC: (.8/3)*1*3=.8; A: .8*2*.5=.8.
	for _, part := range []string{"A,0.80,entity A", "A,B,2.40,relation AB", "B,C,0.80,", "community report", "evidence"} {
		if !strings.Contains(result.Content, part) {
			t.Fatalf("missing %q in %s", part, result.Content)
		}
	}
	if embedDriver.calls != 1 {
		t.Fatal("entity and relation queries did not share one embedding", embedDriver.calls)
	}
	if result.KBID != "kb-a" || driver.context != t.Context() || api.Context == t.Context() {
		t.Fatal("request binding or identity lost")
	}
	for _, budget := range []int{0, 1, 15, 60, 200} {
		row, err := NewPipeline(&pipelineStore{}, &kgReader{}).Retrieval(t.Context(), "owner", []string{"kb-a"}, "A", nil, nil, WithMaxTokens(budget))
		if err != nil || NumTokensFromString(row.Content) > budget {
			t.Fatal(budget, row, err)
		}
	}
}

func TestPipelineRejectsBeforeModelOrEngineIO(t *testing.T) {
	driver := &rewriteDriver{answer: `{"answer_type_keywords":[],"entities_from_query":[]}`}
	name := "chat"
	chat := models.NewChatModel(driver, &name, nil)
	store := &pipelineStore{}
	p := NewPipeline(store, &kgReader{deny: "kb-a"})
	if _, err := p.Retrieval(t.Context(), "owner", []string{"kb-a"}, "A", chat, nil); err == nil {
		t.Fatal("private KB accepted")
	}
	if driver.calls != 0 || len(store.requests) != 0 {
		t.Fatal("rejected scope reached IO")
	}
	p = NewPipeline(store, &kgReader{})
	for _, ids := range [][]string{nil, {}, {"kb-a", "kb-b"}} {
		if _, err := p.Retrieval(t.Context(), "owner", ids, "A", chat, nil); err == nil {
			t.Fatal(ids)
		}
	}
	for _, v := range []float64{math.NaN(), math.Inf(1), -1, 1.1} {
		if _, err := p.Retrieval(t.Context(), "owner", []string{"kb-a"}, "A", chat, nil, WithSimThreshold(v)); err == nil {
			t.Fatal(v)
		}
	}
	result, err := p.Retrieval(t.Context(), "owner", []string{"kb-a"}, "A", chat, nil)
	if err != nil || result.Content != "" || len(store.requests) != 1 || store.requests[0].Filter["knowledge_graph_kwd"] != "ty2ents" {
		t.Fatal("explicit empty rewrite broadened", result, err, store.requests)
	}
}

func TestPipelineFailureCancelsAndDrainsEveryBranch(t *testing.T) {
	store := &pipelineStore{failure: "relation", entered: make(chan struct{}), drained: make(chan struct{})}
	done := make(chan error, 1)
	go func() {
		_, err := NewPipeline(store, &kgReader{}).Retrieval(t.Context(), "owner", []string{"kb-a"}, "A", nil, nil)
		done <- err
	}()
	select {
	case err := <-done:
		if err == nil || !strings.Contains(err.Error(), "owned graph failure") {
			t.Fatal(err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("pipeline failed to drain")
	}
	select {
	case <-store.drained:
	default:
		t.Fatal("returned before canceled branch drained")
	}
}

func TestPipelineRewriteUsesRealBoundOpenAIHTTP(t *testing.T) {
	fixture := testsupport.RegisteredHTTPServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" || r.URL.Path != "/v1/chat/completions" {
			t.Error(r.Method, r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"choices":[{"message":{"content":"{\"answer_type_keywords\":[],\"entities_from_query\":[]}"}}]}`)
	}))
	name := "owned-chat"
	key := "owned-rewrite-fixture-key"
	api := &models.APIConfig{Context: context.Background(), APIKey: &key}
	model := models.NewChatModel(models.NewOpenAIModel(map[string]string{"default": fixture.URL + "/v1"}, models.URLSuffix{Chat: "chat/completions"}), &name, api)
	result, err := NewPipeline(&pipelineStore{}, &kgReader{}).Retrieval(t.Context(), "owner", []string{"kb-a"}, "A", model, nil)
	if err != nil || result.Content != "" || api.Context == t.Context() {
		t.Fatal(result, err)
	}
}

func TestNeighborsRejectMismatchedAndNonfinitePaths(t *testing.T) {
	for _, raw := range []string{"", `null`, `[{"path":["A","B"],"weights":[]}]`, `[{"path":["A",""],"weights":[1]}]`, `[{"path":["A","B"],"weights":[1e999]}]`} {
		if len(parseNeighbors(raw)) != 0 {
			t.Fatal(raw)
		}
	}
}

func TestPipelineCanonicalPathsFuseReverseRelations(t *testing.T) {
	directed := map[Edge]EdgeScore{{"A", "B"}: {Sim: .4, PageRank: 3}, {"B", "A"}: {Sim: .2, PageRank: 7}}
	paths := canonicalPaths(directed)
	if len(paths) != 1 || math.Abs(paths[Edge{"A", "B"}].Sim-.6) > 1e-12 || paths[Edge{"A", "B"}].PageRank != 7 {
		t.Fatal(paths)
	}
	relation := map[Edge]*KGRelation{{"A", "B"}: {Sim: .5, PageRank: 3, Description: "canonical text relation"}}
	fused := FuseRelationScores(relation, nil, paths)
	if len(fused) != 1 || math.Abs(fused[Edge{"A", "B"}].Sim-.8) > 1e-12 {
		t.Fatal("reverse paths did not merge into canonical text relation", fused)
	}
	if len(directed) != 2 || relation[Edge{"A", "B"}].Sim != .5 {
		t.Fatal("input was mutated")
	}
}
