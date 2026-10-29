package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net"
	"reflect"
	"testing"

	"multirag/internal/engine/types"
)

type citationVectorStore struct {
	calls int
	read  func(context.Context, *types.SearchRequest) (*types.SearchResult, error)
}

func (s *citationVectorStore) Search(ctx context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	s.calls++
	return s.read(ctx, req)
}

func citationVectorSelection() chunkVectorScope {
	return chunkVectorScope{datasetID: "kb", tenantID: "tenant", documentIDs: []string{"doc"}, chunkIDs: []string{"one", "two", "missing", "without-vector"}}
}

func vectorRow(id string, raw interface{}) map[string]interface{} {
	return map[string]interface{}{"id": id, "doc_id": "doc", "kb_id": "kb", "q_2_vec": raw}
}

func TestCitationVectorsHydrateOneExactSelection(t *testing.T) {
	ctx := context.WithValue(t.Context(), struct{}{}, "request")
	input := []float64{1.25, -2.5}
	store := &citationVectorStore{read: func(gotCtx context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
		if gotCtx != ctx || !reflect.DeepEqual(req.IndexNames, []string{"multirag_tenant"}) || !reflect.DeepEqual(req.KbIDs, []string{"kb"}) || req.Limit != 4 || req.Offset != 0 || req.IncludeUnavailable {
			t.Fatalf("lost context or explicit dataset/index scope: %+v", req)
		}
		if !reflect.DeepEqual(req.SelectFields, []string{"id", "kb_id", "doc_id", "q_2_vec"}) || !reflect.DeepEqual(req.Filter, map[string]interface{}{"id": []string{"one", "two", "missing", "without-vector"}, "doc_id": []string{"doc"}, "available_int": 1}) {
			t.Fatalf("lost candidate/document restriction or projected fields: %+v", req)
		}
		missingField := vectorRow("without-vector", nil)
		delete(missingField, "q_2_vec")
		return &types.SearchResult{Total: 3, Chunks: []map[string]interface{}{vectorRow("one", input), vectorRow("two", "0\t0"), missingField}}, nil
	}}
	scope := citationVectorSelection()
	scope.chunkIDs = append(scope.chunkIDs, "one")
	result, err := fetchChunkVectors(ctx, store, scope, 2, false)
	if err != nil || store.calls != 1 {
		t.Fatal(result, err, store.calls)
	}
	if !reflect.DeepEqual(result.Vectors["one"], input) || !reflect.DeepEqual(result.Vectors["two"], []float64{0, 0}) || len(result.Vectors) != 4 {
		t.Fatal(result)
	}
	if !reflect.DeepEqual(result.Fallback, map[string]ChunkVectorFallback{"missing": ChunkVectorMissing, "without-vector": ChunkVectorMissingField}) {
		t.Fatal("actual zero vectors must remain distinguishable from fallback", result.Fallback)
	}
	result.Vectors["one"][0] = 10
	result.Vectors["missing"][0] = 11
	if input[0] != 1.25 || result.Vectors["two"][0] != 0 || result.Vectors["without-vector"][0] != 0 {
		t.Fatal("returned vectors alias source or another vector")
	}
}

func TestCitationVectorsRejectUnboundScopeBeforeIO(t *testing.T) {
	for name, mutate := range map[string]func(*chunkVectorScope){
		"missing dataset": func(s *chunkVectorScope) { s.datasetID = "" },
		"missing tenant":  func(s *chunkVectorScope) { s.tenantID = "" },
		"no documents":    func(s *chunkVectorScope) { s.documentIDs = nil },
		"empty documents": func(s *chunkVectorScope) { s.documentIDs = []string{} },
		"blank document":  func(s *chunkVectorScope) { s.documentIDs = []string{" "} },
		"padded dataset":  func(s *chunkVectorScope) { s.datasetID = " kb" },
		"blank chunk":     func(s *chunkVectorScope) { s.chunkIDs = []string{""} },
	} {
		t.Run(name, func(t *testing.T) {
			scope := citationVectorSelection()
			mutate(&scope)
			store := &citationVectorStore{read: func(context.Context, *types.SearchRequest) (*types.SearchResult, error) {
				t.Fatal("unbound read")
				return nil, nil
			}}
			if result, err := fetchChunkVectors(t.Context(), store, scope, 2, true); !errors.Is(err, ErrChunkVectorScope) || result != nil || store.calls != 0 {
				t.Fatal(result, err)
			}
		})
	}
	for _, dim := range []int{-1, 0, 65537} {
		if _, err := fetchChunkVectors(t.Context(), nil, citationVectorSelection(), dim, true); !errors.Is(err, ErrChunkVectorScope) {
			t.Fatal(dim, err)
		}
	}
}

func TestCitationVectorsRejectEngineScopeEscapeAndMalformedRows(t *testing.T) {
	for name, rows := range map[string][]map[string]interface{}{
		"foreign chunk":     {vectorRow("foreign", []float64{1, 2})},
		"foreign document":  {{"id": "one", "doc_id": "foreign", "kb_id": "kb", "q_2_vec": []float64{1, 2}}},
		"foreign dataset":   {{"id": "one", "doc_id": "doc", "kb_id": "foreign", "q_2_vec": []float64{1, 2}}},
		"multiple datasets": {{"id": "one", "doc_id": "doc", "kb_id": []string{"kb", "other"}, "q_2_vec": []float64{1, 2}}},
		"missing identity":  {{"id": "one", "q_2_vec": []float64{1, 2}}},
		"duplicate chunk":   {vectorRow("one", []float64{1, 2}), vectorRow("one", []float64{1, 2})},
		"dimension":         {vectorRow("one", []float64{1})},
		"null field":        {vectorRow("one", nil)},
		"nonfinite":         {vectorRow("one", []float64{math.NaN(), 2})},
	} {
		t.Run(name, func(t *testing.T) {
			store := &citationVectorStore{read: func(context.Context, *types.SearchRequest) (*types.SearchResult, error) {
				return &types.SearchResult{Total: int64(len(rows)), Chunks: rows}, nil
			}}
			if result, err := fetchChunkVectors(t.Context(), store, citationVectorSelection(), 2, true); err == nil || result != nil || store.calls != 1 {
				t.Fatal("malformed/out-of-scope response became fallback", result, err)
			}
		})
	}
}

func TestCitationVectorFormatsAndFiniteValues(t *testing.T) {
	for _, raw := range []interface{}{" 1.25\t-2.5 ", []float64{1.25, -2.5}, []float32{1.25, -2.5}, []interface{}{json.Number("1.25"), float32(-2.5)}, []interface{}{float64(1.25), "-2.5"}} {
		got, err := parseChunkVector(raw, 2)
		if err != nil || !reflect.DeepEqual(got, []float64{1.25, -2.5}) {
			t.Fatal(raw, got, err)
		}
	}
	for _, raw := range []interface{}{"1", "1\t2\t3", "NaN\t1", "1\t+Inf", []float64{math.Inf(-1), 2}, []float32{float32(math.NaN()), 2}, []interface{}{true, 1.0}, []interface{}{json.Number("1e9999"), 2.0}, []interface{}{nil, 2.0}, []interface{}{1, 2}, "\t2"} {
		if got, err := parseChunkVector(raw, 2); !errors.Is(err, ErrChunkVectorData) || got != nil {
			t.Fatal("invalid value accepted", raw, got, err)
		}
	}
}

func TestCitationVectorsTransportFallbackIsExplicitAndOptIn(t *testing.T) {
	failure := &net.OpError{Op: "read", Net: "tcp", Err: errors.New("connection reset")}
	store := &citationVectorStore{read: func(context.Context, *types.SearchRequest) (*types.SearchResult, error) { return nil, failure }}
	if result, err := fetchChunkVectors(t.Context(), store, citationVectorSelection(), 2, false); !errors.Is(err, failure) || result != nil {
		t.Fatal(result, err)
	}
	result, err := fetchChunkVectors(t.Context(), store, citationVectorSelection(), 2, true)
	if err != nil || len(result.Vectors) != 4 || len(result.Fallback) != 4 {
		t.Fatal(result, err)
	}
	for id, vector := range result.Vectors {
		if !reflect.DeepEqual(vector, []float64{0, 0}) || result.Fallback[id] != ChunkVectorTransportFail {
			t.Fatal(id, vector, result.Fallback)
		}
	}
	for _, failure := range []error{ErrDatasetAccessDenied, fmt.Errorf("wrapped: %w", ErrDatasetAccessDenied), errors.New("Elasticsearch request failed with HTTP 403"), context.Canceled, context.DeadlineExceeded, errors.New("invalid index")} {
		store.read = func(context.Context, *types.SearchRequest) (*types.SearchResult, error) { return nil, failure }
		if result, err := fetchChunkVectors(t.Context(), store, citationVectorSelection(), 2, true); !errors.Is(err, failure) || result != nil {
			t.Fatal("nontransport error was hidden", result, err)
		}
	}
}

func TestCitationVectorsCancellationAndMissingEngine(t *testing.T) {
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	store := &citationVectorStore{read: func(context.Context, *types.SearchRequest) (*types.SearchResult, error) {
		t.Fatal("cancelled read")
		return nil, nil
	}}
	if result, err := fetchChunkVectors(ctx, store, citationVectorSelection(), 2, true); !errors.Is(err, context.Canceled) || result != nil || store.calls != 0 {
		t.Fatal(result, err)
	}
	ctx, cancel = context.WithCancel(t.Context())
	store.read = func(context.Context, *types.SearchRequest) (*types.SearchResult, error) {
		cancel()
		return &types.SearchResult{}, nil
	}
	if result, err := fetchChunkVectors(ctx, store, citationVectorSelection(), 2, true); !errors.Is(err, context.Canceled) || result != nil {
		t.Fatal("cancelled response became fallback", result, err)
	}
	if result, err := fetchChunkVectors(t.Context(), nil, citationVectorSelection(), 2, true); err == nil || result != nil {
		t.Fatal(result, err)
	}
	store.read = func(context.Context, *types.SearchRequest) (*types.SearchResult, error) { return nil, nil }
	if result, err := fetchChunkVectors(t.Context(), store, citationVectorSelection(), 2, true); !errors.Is(err, ErrChunkVectorData) || result != nil {
		t.Fatal(result, err)
	}
	scope := citationVectorSelection()
	scope.chunkIDs = nil
	if result, err := fetchChunkVectors(t.Context(), nil, scope, 2, false); err != nil || len(result.Vectors) != 0 || len(result.Fallback) != 0 {
		t.Fatal(result, err)
	}
}

func TestCitationVectorServiceRequiresAuthorization(t *testing.T) {
	var svc *ChunkService
	for _, request := range []*ChunkVectorRequest{nil, {DatasetID: "kb", Dimension: 2}, {DatasetID: "kb", DocumentIDs: []string{"doc"}, ChunkIDs: []string{"one"}, Dimension: 0}} {
		if result, err := svc.FetchChunkVectors(t.Context(), request, "actor"); !errors.Is(err, ErrChunkVectorScope) || result != nil {
			t.Fatal(result, err)
		}
	}
	request := &ChunkVectorRequest{DatasetID: "kb", DocumentIDs: []string{"doc"}, ChunkIDs: []string{"one"}, Dimension: 2, AllowTransportFallback: true}
	if result, err := svc.FetchChunkVectors(t.Context(), request, "actor"); err == nil || result != nil {
		t.Fatal("missing authorization became fallback", result, err)
	}
}

func TestCitationVectorsRejectIncompleteEnginePage(t *testing.T) {
	store := &citationVectorStore{read: func(context.Context, *types.SearchRequest) (*types.SearchResult, error) {
		return &types.SearchResult{Total: 2, Chunks: []map[string]interface{}{vectorRow("one", []float64{1, 2})}}, nil
	}}
	if got, err := fetchChunkVectors(t.Context(), store, citationVectorSelection(), 2, true); !errors.Is(err, ErrChunkVectorData) || got != nil {
		t.Fatal("incomplete matches became missing fallback", got, err)
	}
}
