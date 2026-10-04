package nlp

import (
	"context"
	"errors"
	"path/filepath"
	"reflect"
	"testing"

	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

type retrievalDocumentReader struct {
	docs  []*entity.Document
	err   error
	calls [][]string
}

func (r *retrievalDocumentReader) GetByIDs(ctx context.Context, ids []string) ([]*entity.Document, error) {
	r.calls = append(r.calls, append([]string(nil), ids...))
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return r.docs, r.err
}

type retrievalKBReader struct {
	ids   []string
	err   error
	calls [][]string
}

func (r *retrievalKBReader) GetExistingIDs(_ context.Context, ids []string) ([]string, error) {
	r.calls = append(r.calls, append([]string(nil), ids...))
	return r.ids, r.err
}

func candidateResult(chunks ...map[string]interface{}) *RetrievalSearchResult {
	result := &RetrievalSearchResult{Total: 500, Chunks: chunks, Field: map[string]map[string]interface{}{}, Highlight: map[string]string{}, QueryVector: []float64{1, 2}, Keywords: []string{"question"}, Aggregation: []map[string]interface{}{{"key": "original", "count": 500}}, Options: map[string]interface{}{"total": 500}}
	for _, chunk := range chunks {
		id := chunk["id"].(string)
		result.IDs = append(result.IDs, id)
		result.Field[id] = chunk
		result.Highlight[id] = "highlight " + id
	}
	return result
}

func retrievalChunk(id, docID, kbID string) map[string]interface{} {
	return map[string]interface{}{"id": id, "doc_id": docID, "kb_id": kbID, "docnm_kwd": docID, "content_ltks": id, "content_with_weight": id}
}

func TestPruneDeletedChunksKeepsAlignedCandidatesAndBackendTotal(t *testing.T) {
	docs := &retrievalDocumentReader{docs: []*entity.Document{{ID: "live", KbID: "selected"}}}
	kbs := &retrievalKBReader{ids: []string{"selected"}}
	svc := &RetrievalService{documentDAO: docs, kbDAO: kbs}
	fileSummary := retrievalChunk("file", "live", "selected")
	fileSummary["raptor_kwd"] = "raptor"
	summary := retrievalChunk("summary", "graph_raptor_x", "selected")
	summary["raptor_kwd"] = []interface{}{"raptor"}
	summary["doc_id"] = []string{"graph_raptor_x"}
	invalidSummary := retrievalChunk("invalid-summary", "graph_raptor_x", "deleted-kb")
	invalidSummary["raptor_kwd"] = "raptor"
	input := candidateResult(retrievalChunk("stale", "deleted", "selected"), retrievalChunk("first", "live", "selected"), fileSummary, summary,
		retrievalChunk("missing-doc", "", "selected"), retrievalChunk("mismatch", "live", "deleted-kb"), invalidSummary,
		retrievalChunk("unmarked", "graph_raptor_x", "selected"), retrievalChunk("foreign", "other", "foreign-kb"))
	input.IDs = append(input.IDs, "missing-field")
	input.Field["unused-stale"] = retrievalChunk("unused-stale", "unused-doc", "selected")
	input.Highlight["missing-field"] = "stale highlight"
	got, err := svc.pruneDeletedChunks(context.Background(), input, []string{"selected", "deleted-kb"})
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"first", "file", "summary"}
	if !reflect.DeepEqual(got.IDs, want) || len(got.Field) != len(want) || len(got.Highlight) != len(want) || len(got.Chunks) != len(want) {
		t.Fatalf("misaligned survivors: %#v", got)
	}
	for i, id := range want {
		if got.Chunks[i]["id"] != id || got.Field[id]["id"] != id || got.Highlight[id] != "highlight "+id {
			t.Fatalf("candidate %s misaligned", id)
		}
	}
	if got.Total != 500 || !reflect.DeepEqual(got.QueryVector, input.QueryVector) || !reflect.DeepEqual(got.Options, input.Options) || !reflect.DeepEqual(got.Aggregation, input.Aggregation) || !reflect.DeepEqual(got.Keywords, input.Keywords) {
		t.Fatal("backend search metadata changed")
	}
	if !reflect.DeepEqual(docs.calls, [][]string{{"deleted", "live", "graph_raptor_x"}}) || !reflect.DeepEqual(kbs.calls, [][]string{{"selected", "deleted-kb"}}) {
		t.Fatalf("unscoped or duplicate lookup: docs=%v kbs=%v", docs.calls, kbs.calls)
	}
	if len(input.IDs) != 10 || len(input.Field) != 10 || input.Total != 500 {
		t.Fatal("original search result mutated")
	}
	// A positive existence result must not survive a committed deletion between calls.
	docs.docs, kbs.ids = nil, nil
	got, err = svc.pruneDeletedChunks(context.Background(), input, []string{"selected", "deleted-kb"})
	if err != nil || len(got.IDs) != 0 || got.Total != 500 || len(docs.calls) != 2 || len(kbs.calls) != 2 {
		t.Fatalf("cached parent or total corruption: %#v %v", got, err)
	}
}

func TestPruneDeletedChunksErrorsAndUnverifiableCandidates(t *testing.T) {
	boom := errors.New("SQL unavailable")
	for _, tc := range []struct {
		name  string
		docs  documentReader
		kbs   knowledgebaseReader
		chunk map[string]interface{}
	}{
		{"document-error", &retrievalDocumentReader{err: boom}, nil, retrievalChunk("one", "doc", "kb")},
		{"missing-document-reader", nil, nil, retrievalChunk("one", "doc", "kb")},
		{"dataset-error", nil, &retrievalKBReader{err: boom}, map[string]interface{}{"id": "one", "doc_id": "graph_raptor_x", "kb_id": "kb", "raptor_kwd": "raptor"}},
		{"missing-dataset-reader", nil, nil, map[string]interface{}{"id": "one", "doc_id": "graph_raptor_x", "kb_id": "kb", "raptor_kwd": "raptor"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			svc := &RetrievalService{documentDAO: tc.docs, kbDAO: tc.kbs}
			if got, err := svc.pruneDeletedChunks(context.Background(), candidateResult(tc.chunk), []string{"kb"}); err == nil || got != nil {
				t.Fatalf("unverified results served: %v %v", got, err)
			}
		})
	}
	svc := &RetrievalService{}
	for _, docID := range []interface{}{nil, "", []string{"one", "two"}, []interface{}{123}} {
		chunk := map[string]interface{}{"id": "one", "doc_id": docID, "kb_id": "kb"}
		if got, err := svc.pruneDeletedChunks(context.Background(), candidateResult(chunk), []string{"kb"}); err != nil || len(got.IDs) != 0 {
			t.Fatalf("unverifiable doc ID accepted: %v %v", got, err)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := svc.pruneDeletedChunks(ctx, candidateResult(), nil); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancellation lost: %v", err)
	}
	// A live document in a different SQL dataset is not this chunk's parent.
	svc.documentDAO = &retrievalDocumentReader{docs: []*entity.Document{{ID: "doc", KbID: "other"}}}
	got, err := svc.pruneDeletedChunks(context.Background(), candidateResult(retrievalChunk("one", "doc", "kb")), []string{"kb"})
	if err != nil || len(got.IDs) != 0 {
		t.Fatalf("cross-dataset document accepted: %v %v", got, err)
	}
}

type retrievalTestEngine struct {
	engine.DocEngine
	result   *RetrievalSearchResult
	requests []*types.SearchRequest
}

func (e *retrievalTestEngine) Search(_ context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	e.requests = append(e.requests, req)
	return &types.SearchResult{Chunks: e.result.Chunks, Total: e.result.Total}, nil
}
func (e *retrievalTestEngine) GetDocIDs(_ []map[string]interface{}) []string { return e.result.IDs }
func (e *retrievalTestEngine) GetFields(_ []map[string]interface{}, _ []string) map[string]map[string]interface{} {
	return e.result.Field
}
func (e *retrievalTestEngine) GetAggregation(_ []map[string]interface{}, _ string) []map[string]interface{} {
	return e.result.Aggregation
}
func (e *retrievalTestEngine) GetHighlight(_ []map[string]interface{}, _ []string, _ string) map[string]string {
	return e.result.Highlight
}

type retrievalRerankDriver struct {
	models.ModelDriver
	calls [][]string
}

func (d *retrievalRerankDriver) Rerank(_ *string, _ string, texts []string, _ *models.APIConfig) ([]float64, error) {
	d.calls = append(d.calls, append([]string(nil), texts...))
	scores := make([]float64, len(texts))
	for i := range scores {
		scores[i] = 1 - float64(i)/10
	}
	return scores, nil
}

func TestRetrievalPrunesBeforeRerankWithoutChangingWindowOrCounts(t *testing.T) {
	if err := InitQueryBuilder(filepath.Join("..", "..", "..", "resource", "wordnet")); err != nil {
		t.Fatal(err)
	}
	engine := &retrievalTestEngine{result: candidateResult(retrievalChunk("stale", "gone", "kb"), retrievalChunk("a", "live", "kb"), retrievalChunk("b", "live", "kb"), retrievalChunk("c", "live", "kb"), retrievalChunk("d", "live", "kb"))}
	docs := &retrievalDocumentReader{docs: []*entity.Document{{ID: "live", KbID: "kb"}}}
	driver := &retrievalRerankDriver{}
	svc := NewRetrievalService(engine)
	svc.documentDAO = docs
	top, weight, highlight := 6, 1.0, true
	req := &RetrievalRequest{TenantIDs: []string{"tenant"}, KbIDs: []string{"kb"}, DocIDs: []string{"live", "gone"}, Question: "question", Page: 2, PageSize: 2, Top: &top, VectorSimilarityWeight: &weight, Highlight: &highlight, RankFeature: &map[string]float64{}, RerankModel: models.NewRerankModel(driver, nil, nil)}
	got, err := svc.Retrieval(context.Background(), req)
	if err != nil || len(got.Chunks) != 2 || got.Chunks[0]["chunk_id"] != "c" || got.Chunks[1]["chunk_id"] != "d" {
		t.Fatalf("page shifted: %#v %v", got, err)
	}
	if !reflect.DeepEqual(driver.calls, [][]string{{"a", "b", "c", "d"}}) || got.Chunks[0]["highlight"] != "highlight c" || len(got.DocAggs) != 1 || got.DocAggs[0]["count"] != 4 {
		t.Fatalf("stale scoring/highlight/aggregation: calls=%v got=%#v", driver.calls, got)
	}
	search := engine.requests[0]
	if search.Offset != 0 || search.Limit != 6 || !reflect.DeepEqual(search.KbIDs, req.KbIDs) || !reflect.DeepEqual(search.Filter["doc_id"], req.DocIDs) || !reflect.DeepEqual(search.IndexNames, []string{"multirag_tenant"}) {
		t.Fatalf("search scope/window changed: %#v", search)
	}
	engine.result = candidateResult(retrievalChunk("stale2", "gone", "kb"), retrievalChunk("late", "live", "kb"))
	req.Page = 4
	got, err = svc.Retrieval(context.Background(), req)
	if err != nil || len(got.Chunks) != 1 || got.Chunks[0]["chunk_id"] != "late" || engine.requests[1].Offset != 6 || len(engine.requests) != 2 {
		t.Fatalf("window refill or short page changed: %#v %v", got, err)
	}
	docs.docs = nil
	before := len(driver.calls)
	got, err = svc.Retrieval(context.Background(), req)
	if err != nil || len(got.Chunks) != 0 || len(got.DocAggs) != 0 || len(driver.calls) != before {
		t.Fatalf("all-deleted window reached reranker: %#v %v", got, err)
	}
	docs.err = errors.New("lookup failed")
	if got, err = svc.Retrieval(context.Background(), req); err == nil || got != nil || len(driver.calls) != before {
		t.Fatalf("lookup failure reached reranker: %#v %v", got, err)
	}
}

type retrievalParentEngine struct {
	engine.DocEngine
	parent map[string]interface{}
	scopes [][]string
}

func (e *retrievalParentEngine) GetChunk(_ context.Context, _ string, _ string, kbIDs []string) (interface{}, error) {
	e.scopes = append(e.scopes, kbIDs)
	return e.parent, nil
}

func TestRetrievalByChildrenCannotReintroduceForeignOrDeletedParent(t *testing.T) {
	for _, tc := range []struct{ name, docID, kbID string }{
		{"same-document", "live", "kb"}, {"deleted-document", "gone", "kb"}, {"foreign-dataset", "live", "other"}, {"missing-parent-document", "", "kb"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			child := retrievalChunk("child", "live", "kb")
			child["mom_id"], child["similarity"], child["highlight"] = "parent", 1.0, "safe highlight"
			parent := retrievalChunk("parent", tc.docID, tc.kbID)
			parent["content_with_weight"] = "parent text"
			e := &retrievalParentEngine{parent: parent}
			got := RetrievalByChildren([]map[string]interface{}{child}, []string{"tenant"}, e, context.Background())
			if len(got) != 1 || !reflect.DeepEqual(e.scopes, [][]string{{"kb"}}) {
				t.Fatalf("scope/count lost: %v %v", got, e.scopes)
			}
			if tc.name == "same-document" {
				if got[0]["chunk_id"] != "parent" || got[0]["content_with_weight"] != "parent text" {
					t.Fatalf("valid parent not expanded: %v", got)
				}
			} else if !reflect.DeepEqual(got[0], child) {
				t.Fatalf("unverified parent served instead of child: %v", got)
			}
		})
	}
	// Legacy mom IDs can collide across documents; keep separate groups.
	first, second := retrievalChunk("a", "live", "kb"), retrievalChunk("b", "other-doc", "kb")
	first["mom_id"], second["mom_id"] = "same-parent", "same-parent"
	e := &retrievalParentEngine{parent: retrievalChunk("same-parent", "live", "kb")}
	got := RetrievalByChildren([]map[string]interface{}{first, second}, []string{"tenant"}, e, context.Background())
	if len(got) != 2 || len(e.scopes) != 2 {
		t.Fatalf("document groups merged: %v", got)
	}
}
