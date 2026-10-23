package service

import (
	"context"
	"errors"
	"reflect"
	"testing"

	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/server"
)

type datasetMetadataFixture struct {
	engine.DocEngine
	request *types.SearchRequest
	context context.Context
	result  *types.SearchResult
	err     error
	calls   int
}

func (f *datasetMetadataFixture) Search(ctx context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	f.calls++
	f.context = ctx
	f.request = req
	return f.result, f.err
}

func TestDatasetDocumentsMetadataSourceAndScope(t *testing.T) {
	stored := entity.JSONMap{"source": "sql"}
	documents := []entity.Document{{ID: "one", MetaFields: &stored}, {ID: "two"}}
	kb := &entity.Knowledgebase{ID: "kb", TenantID: "tenant"}
	standalone := &DocumentService{}
	if result, err := standalone.datasetDocumentMetadata(t.Context(), kb, documents); err != nil || result["one"]["source"] != "sql" {
		t.Fatal(result, err)
	}
	standalone.engineType = server.EngineInfinity
	if _, err := standalone.datasetDocumentMetadata(t.Context(), kb, documents); err == nil {
		t.Fatal("configured missing engine silently used SQL")
	}
	fixture := &datasetMetadataFixture{result: &types.SearchResult{Chunks: []map[string]any{{"doc_id": "one", "meta_fields": map[string]any{"source": "configured-engine"}}, {"doc_id": "two", "meta_fields": map[string]any{}}}}}
	svc := &DocumentService{docEngine: fixture, engineType: server.EngineInfinity}
	result, err := svc.datasetDocumentMetadata(t.Context(), kb, documents)
	if err != nil || result["one"]["source"] != "configured-engine" || fixture.calls != 1 {
		t.Fatal(result, err, fixture.calls)
	}
	if fixture.context != t.Context() || !reflect.DeepEqual(fixture.request.IndexNames, []string{"multirag_doc_meta_tenant"}) || !reflect.DeepEqual(fixture.request.KbIDs, []string{"kb"}) || !reflect.DeepEqual(fixture.request.Filter["doc_id"], []string{"one", "two"}) {
		t.Fatal("metadata query escaped page/tenant or context")
	}
	fixture.err = errors.New("private engine error")
	if _, err := svc.datasetDocumentMetadata(t.Context(), kb, documents); err == nil {
		t.Fatal("engine failure silently fell back to SQL")
	}
	fixture.err = nil
	for _, chunks := range [][]map[string]any{{{"doc_id": "foreign", "meta_fields": map[string]any{}}}, {{"doc_id": "one"}, {"doc_id": "one"}}} {
		fixture.result = &types.SearchResult{Chunks: chunks}
		if _, err := svc.datasetDocumentMetadata(t.Context(), kb, documents); err == nil {
			t.Fatal("out-of-scope/duplicate metadata accepted")
		}
	}
}
