package service

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"testing"

	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
)

type referenceMetadataEngine struct {
	engine.DocEngine
	search func(context.Context, *types.SearchRequest) (*types.SearchResult, error)
}

func (e *referenceMetadataEngine) Search(ctx context.Context, request *types.SearchRequest) (*types.SearchResult, error) {
	return e.search(ctx, request)
}

func referenceMetadataSnapshot(t *testing.T, value interface{}) string {
	t.Helper()
	raw, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}

func TestEnrichReferenceMetadataScopesIdentityAndCopies(t *testing.T) {
	chunks := []map[string]interface{}{
		{"id": "chunk-a1", "doc_id": "shared-doc", "kb_id": "kb-a", "author": "chunk-value", "document_metadata": map[string]interface{}{"stale": true}, "vector": []float64{1, 2}, "raw": []byte{1, 2}},
		{"id": "chunk-a2", "doc_id": "shared-doc", "kb_id": []string{"kb-a"}},
		{"id": "chunk-b", "doc_id": "shared-doc", "kb_id": []interface{}{"kb-b"}},
		{"id": "missing-chunk", "doc_id": "missing-doc", "kb_id": "kb-a", "document_metadata": map[string]interface{}{"stale": true}},
		{"id": "ambiguous", "doc_id": "shared-doc", "kb_id": []string{"kb-a", "kb-b"}, "document_metadata": map[string]interface{}{"stale": true}},
		{"id": "not-a-document", "kb_id": "kb-a", "document_metadata": map[string]interface{}{"stale": true}},
	}
	before := referenceMetadataSnapshot(t, chunks)
	nested := map[string]interface{}{"tags": []interface{}{"owned"}, "labels": []string{"original"}}
	storeFields := map[string]interface{}{"author": "store-a", "nested": nested, "zero": 0, "false": false}
	storeBefore := referenceMetadataSnapshot(t, storeFields)
	var trace []string
	type marker struct{}
	ctx := context.WithValue(context.Background(), marker{}, "request-scope")
	resolve := func(gotContext context.Context, kb, user string) (*entity.Knowledgebase, error) {
		if gotContext.Value(marker{}) != "request-scope" || user != "reader" {
			t.Fatal("resolver lost request context or actor")
		}
		trace = append(trace, "resolve:"+kb)
		return &entity.Knowledgebase{ID: kb, TenantID: "tenant-" + kb}, nil
	}
	svc := &MetadataService{docEngine: &referenceMetadataEngine{search: func(gotContext context.Context, request *types.SearchRequest) (*types.SearchResult, error) {
		if gotContext.Value(marker{}) != "request-scope" {
			t.Fatal("search lost request context")
		}
		if len(trace) < 2 || trace[0] != "resolve:kb-a" || trace[1] != "resolve:kb-b" {
			t.Fatal("search ran before every referenced dataset was authorized")
		}
		if len(request.KbIDs) != 1 {
			t.Fatalf("unexpected KB scope: %#v", request.KbIDs)
		}
		kb := request.KbIDs[0]
		trace = append(trace, "search:"+kb)
		if !reflect.DeepEqual(request.IndexNames, []string{BuildMetadataIndexName("tenant-" + kb)}) || !request.IncludeUnavailable || request.Offset != 0 {
			t.Fatalf("incorrect tenant/policy scope: %#v", request)
		}
		wantIDs := []string{"shared-doc"}
		fields := map[string]interface{}{"author": "store-b"}
		if kb == "kb-a" {
			wantIDs = []string{"missing-doc", "shared-doc"}
			fields = storeFields
		}
		if !reflect.DeepEqual(request.Filter, map[string]interface{}{"id": wantIDs}) || request.Limit != len(wantIDs) {
			t.Fatalf("metadata query did not deduplicate document identities: %#v", request)
		}
		return &types.SearchResult{Total: 1, Chunks: []map[string]interface{}{{"id": "shared-doc", "kb_id": kb, "meta_fields": fields}}}, nil
	}}}
	got, err := svc.enrichChunksWithDocMetadata(ctx, chunks, "reader", nil, resolve)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(trace, []string{"resolve:kb-a", "resolve:kb-b", "search:kb-a", "search:kb-b"}) {
		t.Fatalf("unexpected authorization/search sequence: %#v", trace)
	}
	for index, author := range map[int]string{0: "store-a", 1: "store-a", 2: "store-b"} {
		metadata, ok := got[index]["document_metadata"].(map[string]interface{})
		if !ok || metadata["author"] != author || metadata["stale"] != nil {
			t.Fatalf("wrong metadata for chunk %d: %#v", index, got[index])
		}
	}
	if got[0]["author"] != "chunk-value" {
		t.Fatal("enrichment overwrote original chunk fields")
	}
	for _, index := range []int{3, 4, 5} {
		if _, exists := got[index]["document_metadata"]; exists {
			t.Fatalf("missing or ambiguous document retained stale metadata: %#v", got[index])
		}
	}
	first := got[0]["document_metadata"].(map[string]interface{})
	second := got[1]["document_metadata"].(map[string]interface{})
	first["nested"].(map[string]interface{})["tags"].([]interface{})[0] = "changed"
	first["nested"].(map[string]interface{})["labels"].([]string)[0] = "changed"
	got[0]["vector"].([]float64)[0] = 9
	got[0]["raw"].([]byte)[0] = 9
	got[1]["kb_id"].([]string)[0] = "changed"
	if second["nested"].(map[string]interface{})["tags"].([]interface{})[0] != "owned" || second["nested"].(map[string]interface{})["labels"].([]string)[0] != "original" {
		t.Fatal("different chunks share mutable metadata")
	}
	if before != referenceMetadataSnapshot(t, chunks) || storeBefore != referenceMetadataSnapshot(t, storeFields) {
		t.Fatal("enrichment or returned copies mutated chunk input or source metadata")
	}
}

func TestEnrichReferenceMetadataEmptyFieldsAndMissingSelection(t *testing.T) {
	chunks := []map[string]interface{}{{"id": "chunk", "doc_id": "doc", "kb_id": "kb", "document_metadata": map[string]interface{}{"stale": true}}}
	before := referenceMetadataSnapshot(t, chunks)
	resolves, searches := 0, 0
	resolve := func(context.Context, string, string) (*entity.Knowledgebase, error) {
		resolves++
		return &entity.Knowledgebase{ID: "kb", TenantID: "tenant"}, nil
	}
	svc := &MetadataService{docEngine: &referenceMetadataEngine{search: func(context.Context, *types.SearchRequest) (*types.SearchResult, error) {
		searches++
		return &types.SearchResult{Total: 1, Chunks: []map[string]interface{}{{"id": "doc", "kb_id": "kb", "meta_fields": map[string]interface{}{"author": "source", "other": 0}}}}, nil
	}}}
	for _, fields := range [][]string{nil, {}, {"author"}, {"absent"}} {
		beforeResolves, beforeSearches := resolves, searches
		got, err := svc.enrichChunksWithDocMetadata(context.Background(), chunks, "reader", fields, resolve)
		if err != nil {
			t.Fatal(err)
		}
		metadata, attached := got[0]["document_metadata"].(map[string]interface{})
		switch {
		case fields == nil:
			if !attached || !reflect.DeepEqual(metadata, map[string]interface{}{"author": "source", "other": 0}) {
				t.Fatalf("nil selection must include all metadata: %#v", got)
			}
		case len(fields) == 0:
			if attached || resolves != beforeResolves || searches != beforeSearches {
				t.Fatal("explicit empty selection performed IO or retained stale metadata")
			}
		case fields[0] == "author":
			if !reflect.DeepEqual(metadata, map[string]interface{}{"author": "source"}) {
				t.Fatalf("field selection broadened: %#v", metadata)
			}
		default:
			if attached {
				t.Fatalf("missing field selection retained stale metadata: %#v", metadata)
			}
		}
	}
	if before != referenceMetadataSnapshot(t, chunks) {
		t.Fatal("field selection mutated input")
	}
	if got, err := svc.enrichChunksWithDocMetadata(context.Background(), nil, "reader", nil, resolve); err != nil || got != nil {
		t.Fatalf("nil chunks changed: %#v, %v", got, err)
	}
}

func TestEnrichReferenceMetadataDeniesWholeScopeBeforeSearch(t *testing.T) {
	denied := errors.New("denied dataset")
	chunks := []map[string]interface{}{{"doc_id": "doc", "kb_id": "kb-a"}, {"doc_id": "doc", "kb_id": "kb-b"}}
	before := referenceMetadataSnapshot(t, chunks)
	searches := 0
	svc := &MetadataService{docEngine: &referenceMetadataEngine{search: func(context.Context, *types.SearchRequest) (*types.SearchResult, error) {
		searches++
		return &types.SearchResult{}, nil
	}}}
	for _, fail := range []string{"denied", "nil", "wrong-kb", "empty-tenant"} {
		resolve := func(_ context.Context, kb, _ string) (*entity.Knowledgebase, error) {
			if kb == "kb-a" {
				return &entity.Knowledgebase{ID: kb, TenantID: "tenant-a"}, nil
			}
			switch fail {
			case "denied":
				return nil, denied
			case "nil":
				return nil, nil
			case "wrong-kb":
				return &entity.Knowledgebase{ID: "foreign", TenantID: "tenant-b"}, nil
			default:
				return &entity.Knowledgebase{ID: kb}, nil
			}
		}
		got, err := svc.enrichChunksWithDocMetadata(context.Background(), chunks, "reader", nil, resolve)
		if err == nil || got != nil || searches != 0 || fail == "denied" && !errors.Is(err, denied) {
			t.Fatalf("mixed scope %q not denied atomically: %#v, %v, calls=%d", fail, got, err, searches)
		}
	}
	if before != referenceMetadataSnapshot(t, chunks) {
		t.Fatal("denial mutated input")
	}
}

func TestEnrichReferenceMetadataFailureHasNoPartialResult(t *testing.T) {
	storageErr := errors.New("storage failure")
	chunks := []map[string]interface{}{{"id": "chunk-a", "doc_id": "doc", "kb_id": "kb-a", "document_metadata": map[string]interface{}{"stale": true}}, {"id": "chunk-b", "doc_id": "doc", "kb_id": "kb-b"}}
	before := referenceMetadataSnapshot(t, chunks)
	resolve := func(_ context.Context, kb, _ string) (*entity.Knowledgebase, error) {
		return &entity.Knowledgebase{ID: kb, TenantID: "tenant-" + kb}, nil
	}
	valid := map[string]interface{}{"id": "doc", "kb_id": "kb-b", "meta_fields": map[string]interface{}{"author": "source"}}
	for _, test := range []struct {
		name string
		rows []map[string]interface{}
		err  error
	}{
		{name: "storage", err: storageErr},
		{name: "duplicate", rows: []map[string]interface{}{valid, valid}},
		{name: "foreign document", rows: []map[string]interface{}{{"id": "foreign", "kb_id": "kb-b", "meta_fields": map[string]interface{}{}}}},
		{name: "foreign dataset", rows: []map[string]interface{}{{"id": "doc", "kb_id": "kb-a", "meta_fields": map[string]interface{}{}}}},
		{name: "malformed identity", rows: []map[string]interface{}{{"id": 0, "kb_id": "kb-b", "meta_fields": map[string]interface{}{}}}},
		{name: "chunk identity only", rows: []map[string]interface{}{{"doc_id": "doc", "kb_id": "kb-b", "meta_fields": map[string]interface{}{}}}},
		{name: "malformed fields", rows: []map[string]interface{}{{"id": "doc", "kb_id": "kb-b", "meta_fields": "bad-json"}}},
		{name: "array fields", rows: []map[string]interface{}{{"id": "doc", "kb_id": "kb-b", "meta_fields": []interface{}{}}}},
	} {
		t.Run(test.name, func(t *testing.T) {
			calls := 0
			svc := &MetadataService{docEngine: &referenceMetadataEngine{search: func(_ context.Context, request *types.SearchRequest) (*types.SearchResult, error) {
				calls++
				if request.KbIDs[0] == "kb-a" {
					return &types.SearchResult{Total: 1, Chunks: []map[string]interface{}{{"id": "doc", "kb_id": "kb-a", "meta_fields": map[string]interface{}{"author": "first-store"}}}}, nil
				}
				return &types.SearchResult{Total: int64(len(test.rows)), Chunks: test.rows}, test.err
			}}}
			got, err := svc.enrichChunksWithDocMetadata(context.Background(), chunks, "reader", nil, resolve)
			if err == nil || got != nil || calls != 2 || test.err != nil && !errors.Is(err, test.err) {
				t.Fatalf("failure yielded partial result or lost error: %#v, %v, calls=%d", got, err, calls)
			}
			if before != referenceMetadataSnapshot(t, chunks) {
				t.Fatal("failed enrichment mutated input")
			}
		})
	}
}

func TestEnrichReferenceMetadataCancellation(t *testing.T) {
	chunks := []map[string]interface{}{{"id": "chunk", "doc_id": "doc", "kb_id": "kb", "document_metadata": map[string]interface{}{"stale": true}}}
	before := referenceMetadataSnapshot(t, chunks)
	for _, early := range []bool{true, false} {
		ctx, cancel := context.WithCancel(context.Background())
		resolves, searches := 0, 0
		if early {
			cancel()
		}
		resolve := func(context.Context, string, string) (*entity.Knowledgebase, error) {
			resolves++
			return &entity.Knowledgebase{ID: "kb", TenantID: "tenant"}, nil
		}
		svc := &MetadataService{docEngine: &referenceMetadataEngine{search: func(gotContext context.Context, _ *types.SearchRequest) (*types.SearchResult, error) {
			searches++
			cancel()
			if gotContext.Err() != context.Canceled {
				t.Fatal("search received a detached context")
			}
			// Even a backend that finishes after cancellation cannot return success.
			return &types.SearchResult{Total: 1, Chunks: []map[string]interface{}{{"id": "doc", "kb_id": "kb", "meta_fields": map[string]interface{}{"author": "source"}}}}, nil
		}}}
		got, err := svc.enrichChunksWithDocMetadata(ctx, chunks, "reader", nil, resolve)
		cancel()
		if !errors.Is(err, context.Canceled) || got != nil || early && (resolves != 0 || searches != 0) || !early && (resolves != 1 || searches != 1) {
			t.Fatalf("cancellation escaped: early=%v result=%#v err=%v resolves=%d searches=%d", early, got, err, resolves, searches)
		}
	}
	if before != referenceMetadataSnapshot(t, chunks) {
		t.Fatal("canceled enrichment mutated input")
	}
}
