package nlp

import (
	"context"
	"errors"
	"fmt"
	"math"
	"reflect"
	"testing"

	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

type pagedRetrievalEngine struct {
	retrievalTestEngine
	all []map[string]interface{}
}

func (e *pagedRetrievalEngine) Search(_ context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	e.requests = append(e.requests, req)
	start, end := req.Offset, req.Offset+req.Limit
	if start > len(e.all) {
		start = len(e.all)
	}
	if end > len(e.all) {
		end = len(e.all)
	}
	e.result = candidateResult(e.all[start:end]...)
	e.result.Total = int64(len(e.all))
	return &types.SearchResult{Chunks: e.result.Chunks, Total: e.result.Total}, nil
}

type tiedReranker struct{ models.ModelDriver }

func (*tiedReranker) Rerank(_ *string, _ string, texts []string, _ *models.APIConfig, _ *models.RerankConfig) (*models.RerankResponse, error) {
	response := &models.RerankResponse{Data: make([]models.RerankResult, len(texts))}
	for i := range texts {
		response.Data[i] = models.RerankResult{Index: i, RelevanceScore: .8}
	}
	return response, nil
}

func TestRetrievalWindowGridFetchesEveryCandidateWithoutDrift(t *testing.T) {
	if err := InitQueryBuilder(""); err != nil {
		t.Fatal(err)
	}
	for _, size := range []int{1, 5, 7, 10, 30, 50, 64} {
		for _, top := range []int{0, 5, 30, 50, 55, 64, 100, 1024} {
			t.Run(fmt.Sprintf("size%d_top%d", size, top), func(t *testing.T) {
				e := &pagedRetrievalEngine{}
				const total = 90
				for i := 0; i < total; i++ {
					e.all = append(e.all, retrievalChunk(fmt.Sprint(i), "doc", "kb"))
				}
				svc := NewRetrievalService(e)
				svc.documentDAO = &retrievalDocumentReader{docs: []*entity.Document{{ID: "doc", KbID: "kb"}}}
				weight := 1.0
				aggs := false
				req := &RetrievalRequest{Question: "query", TenantIDs: []string{"tenant"}, KbIDs: []string{"kb"}, PageSize: size, Top: &top, VectorSimilarityWeight: &weight, Aggs: &aggs, RankFeature: &map[string]float64{}, RerankModel: models.NewRerankModel(&tiedReranker{}, nil, nil)}
				var ids []string
				for page := 1; (page-1)*size < total; page++ {
					req.Page = page
					got, err := svc.Retrieval(context.Background(), req)
					if err != nil {
						t.Fatal(err)
					}
					want := size
					if remaining := total - (page-1)*size; remaining < want {
						want = remaining
					}
					if len(got.Chunks) != want || got.Total != total {
						t.Fatalf("page%d: chunks=%d total=%d want%d/%d", page, len(got.Chunks), got.Total, want, total)
					}
					for _, chunk := range got.Chunks {
						ids = append(ids, chunk["chunk_id"].(string))
					}
					search := e.requests[len(e.requests)-1]
					window := types.RerankWindow(size, top)
					if search.Limit != window || search.Offset != ((page-1)*size/window)*window || search.Limit%size != 0 {
						t.Fatalf("unaligned actual engine request: %#v", search)
					}
				}
				for i, id := range ids {
					if id != fmt.Sprint(i) {
						t.Fatalf("gap/duplicate at%d: %s", i, id)
					}
				}
			})
		}
	}
}

func TestRetrievalRejectsInvalidNumbersAndRerankFailure(t *testing.T) {
	if err := InitQueryBuilder(""); err != nil {
		t.Fatal(err)
	}
	e := &retrievalTestEngine{result: candidateResult(retrievalChunk("one", "doc", "kb"))}
	svc := NewRetrievalService(e)
	svc.documentDAO = &retrievalDocumentReader{docs: []*entity.Document{{ID: "doc", KbID: "kb"}}}
	top, weight, threshold := 1024, 1.0, 0.0
	base := RetrievalRequest{Question: "q", TenantIDs: []string{"tenant"}, KbIDs: []string{"kb"}, Page: 1, PageSize: 10, Top: &top, VectorSimilarityWeight: &weight, SimilarityThreshold: &threshold, RankFeature: &map[string]float64{}}
	for _, value := range []float64{math.NaN(), math.Inf(1), -1, 1.01} {
		request := base
		request.VectorSimilarityWeight = &value
		if got, err := svc.Retrieval(context.Background(), &request); err == nil || got != nil {
			t.Fatal("invalid weight accepted", got, err)
		}
		request = base
		request.SimilarityThreshold = &value
		if got, err := svc.Retrieval(context.Background(), &request); err == nil || got != nil {
			t.Fatal("invalid threshold accepted", got, err)
		}
	}
	for _, request := range []RetrievalRequest{
		{Question: "q", Page: 0, PageSize: 10}, {Question: "q", Page: 1, PageSize: 0},
		{Question: "q", Page: int(^uint(0) >> 1), PageSize: 2},
	} {
		if got, err := svc.Retrieval(context.Background(), &request); err == nil || got != nil {
			t.Fatal("invalid pagination accepted", got, err)
		}
	}
	if len(e.requests) != 0 {
		t.Fatal("invalid requests reached engine", e.requests)
	}
	boom := errors.New("provider unavailable")
	driver := &capturedReranker{err: boom}
	request := base
	request.RerankModel = models.NewRerankModel(driver, nil, nil)
	if got, err := svc.Retrieval(context.Background(), &request); !errors.Is(err, boom) || got != nil {
		t.Fatal("rerank failure became success", got, err)
	}
	request.DocIDs = []string{}
	before := len(e.requests)
	if got, err := svc.Retrieval(context.Background(), &request); err != nil || len(got.Chunks) != 0 || len(e.requests) != before {
		t.Fatal("explicit empty scope reached engine", got, err)
	}
}

type failingParentEngine struct {
	retrievalParentEngine
	err    error
	cancel context.CancelFunc
}

func (e *failingParentEngine) GetChunk(ctx context.Context, index, id string, kbIDs []string) (interface{}, error) {
	if e.cancel != nil {
		e.cancel()
	}
	return e.retrievalParentEngine.GetChunk(ctx, index, id, kbIDs)
}

type errorParentEngine struct {
	retrievalParentEngine
	err error
}

func (e *errorParentEngine) GetChunk(context.Context, string, string, []string) (interface{}, error) {
	return nil, e.err
}

func TestRetrievalByChildrenErrorsCancellationAndTies(t *testing.T) {
	child := retrievalChunk("a", "doc", "kb")
	child["chunk_id"], child["mom_id"], child["similarity"] = "a", "parent", .8
	boom := errors.New("store failed")
	if got, err := RetrievalByChildrenChecked(context.Background(), []map[string]interface{}{child}, []string{"tenant"}, &errorParentEngine{err: boom}); !errors.Is(err, boom) || got != nil {
		t.Fatal("parent storage failure swallowed", got, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	e := &failingParentEngine{cancel: cancel}
	if got, err := RetrievalByChildrenChecked(ctx, []map[string]interface{}{child}, []string{"tenant"}, e); !errors.Is(err, context.Canceled) || got != nil {
		t.Fatal("parent cancellation swallowed", got, err)
	}
	parent := retrievalChunk("parent", "doc", "kb")
	plain := retrievalChunk("b", "doc", "kb")
	plain["chunk_id"], plain["similarity"] = "b", .8
	for i := 0; i < 20; i++ {
		got, err := RetrievalByChildrenChecked(context.Background(), []map[string]interface{}{child, plain}, []string{"tenant"}, &retrievalParentEngine{parent: parent})
		if err != nil || len(got) != 2 || !reflect.DeepEqual([]interface{}{got[0]["chunk_id"], got[1]["chunk_id"]}, []interface{}{"parent", "b"}) {
			t.Fatal("tie order changed", got, err)
		}
	}
}

func TestRetrievalDocumentScopeDoesNotBypassNormalizedThreshold(t *testing.T) {
	if err := InitQueryBuilder(""); err != nil {
		t.Fatal(err)
	}
	e := &retrievalTestEngine{result: candidateResult(retrievalChunk("one", "doc", "kb"))}
	svc := NewRetrievalService(e)
	svc.documentDAO = &retrievalDocumentReader{docs: []*entity.Document{{ID: "doc", KbID: "kb"}}}
	weight, threshold := 1.0, .8
	driver := &capturedReranker{scores: []float64{.2}}
	req := &RetrievalRequest{Question: "q", TenantIDs: []string{"tenant"}, KbIDs: []string{"kb"}, DocIDs: []string{"doc"}, Page: 1, PageSize: 10, VectorSimilarityWeight: &weight, SimilarityThreshold: &threshold, RankFeature: &map[string]float64{}, RerankModel: models.NewRerankModel(driver, nil, nil)}
	got, err := svc.Retrieval(context.Background(), req)
	if err != nil || len(got.Chunks) != 0 || got.Total != e.result.Total {
		t.Fatal("document scope bypassed threshold", got, err)
	}
	threshold = .2
	got, err = svc.Retrieval(context.Background(), req)
	if err != nil || len(got.Chunks) != 1 || got.Chunks[0]["vector_similarity"] != .2 {
		t.Fatal("calibrated score scale changed", got, err)
	}
}
