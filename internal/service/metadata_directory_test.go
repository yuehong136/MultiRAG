package service

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"math"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/testsupport/scratch"
	"reflect"
	"testing"
)

type canonicalMetadataEngine struct {
	engine.DocEngine
	response *types.SearchMetadataResult
	failure  error
	request  *types.SearchMetadataRequest
	calls    int
}

func (f *canonicalMetadataEngine) SearchMetadata(ctx context.Context, request *types.SearchMetadataRequest) (*types.SearchMetadataResult, error) {
	f.calls++
	f.request = request
	return f.response, f.failure
}

func TestMetadataDirectoryExactScalarAndEncodedNumbers(t *testing.T) {
	raw := `{"large":9007199254740993,"boolean":false,"zero":0,"empty":""}`
	packed := make([]byte, 4)
	binary.LittleEndian.PutUint32(packed, uint32(len(raw)))
	packed = append(packed, []byte(raw)...)
	for _, value := range []interface{}{raw, []byte(raw), packed} {
		fields, err := ExtractMetaFields(map[string]interface{}{"meta_fields": value})
		if err != nil || fields["large"] != json.Number("9007199254740993") || fields["boolean"] != false || fields["empty"] != "" || fields["zero"] != json.Number("0") {
			t.Fatalf("scalar decode %T %#v %v", value, fields, err)
		}
	}
}
func TestMetadataReferencePublicEmptySelectionNeedsNoDependencies(t *testing.T) {
	s := &MetadataService{}
	got, err := s.GetFlattedMetaByKBs([]string{})
	if err != nil || got == nil || len(got) != 0 {
		t.Fatal(got, err)
	}
	s.EnrichChunksWithDocMetadata(nil, "owner", nil)
}
func TestMetadataReferenceCopyPreservesNullValues(t *testing.T) {
	values := map[string]interface{}{"list": []interface{}(nil), "object": map[string]interface{}(nil), "empty": []interface{}{}}
	chunks := []map[string]interface{}{{"kb_id": "kb", "doc_id": "doc"}}
	AttachDocMetaToChunks(chunks, DocMetaMap{"doc": values}, nil)
	raw, err := json.Marshal(chunks[0]["document_metadata"])
	if err != nil || string(raw) != `{"empty":[],"list":null,"object":null}` {
		t.Fatal(string(raw), err)
	}
}
func TestMetadataDatasetFlattenAcceptsExactNumbers(t *testing.T) {
	f := scratch.New(t)
	old := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = old })
	if err := f.DB.AutoMigrate(&entity.Knowledgebase{}); err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Create(&entity.Knowledgebase{ID: "kb", TenantID: "owner", Name: "Metadata", CreatedBy: "owner", EmbdID: "m", Status: func() *string { value := "1"; return &value }()}).Error; err != nil {
		t.Fatal(err)
	}
	index := &canonicalMetadataEngine{response: &types.SearchMetadataResult{MetadataRecords: []map[string]interface{}{{"id": "doc", "kb_id": "kb", "meta_fields": map[string]interface{}{"large": json.Number("9007199254740993"), "zero": 0, "flag": false, "empty": "", "list": []interface{}{json.Number("0"), false, nil}}}}}}
	s := &MetadataService{docEngine: index}
	got, err := s.GetFlattedMetaByKBs([]string{"kb"})
	if err != nil {
		t.Fatal(err)
	}
	for key, value := range map[string]string{"large": "9007199254740993", "zero": "0", "flag": "false", "empty": ""} {
		if !reflect.DeepEqual(got[key][value], []string{"doc"}) {
			t.Fatal("scalar lost", key, got)
		}
	}
	if index.request.TenantID != "owner" || !reflect.DeepEqual(index.request.Filter["kb_id"], []string{"kb"}) || index.request.Limit != 10000 {
		t.Fatal("canonical metadata tenant/dataset query changed", index.request)
	}
	for _, rows := range [][]map[string]interface{}{
		{{"id": "doc", "kb_id": "foreign", "meta_fields": map[string]interface{}{}}},
		{{"id": "doc", "kb_id": "kb", "meta_fields": "{bad"}},
		{{"id": "doc", "kb_id": "kb", "meta_fields": map[string]interface{}{"bad": math.NaN()}}},
		{{"id": "doc", "kb_id": "kb"}, {"id": "doc", "kb_id": "kb"}},
	} {
		index.response = &types.SearchMetadataResult{MetadataRecords: rows}
		if values, err := s.GetFlattedMetaByKBs([]string{"kb"}); err == nil || values != nil {
			t.Fatal("invalid metadata became empty/partial success", values, err)
		}
	}
	index.response = nil
	if got, err := s.GetFlattedMetaByKBs([]string{"kb"}); err == nil || got != nil {
		t.Fatal("nil metadata response acknowledged")
	}
	failure := errors.New("controlled engine failure")
	index.failure = failure
	if got, err := s.GetFlattedMetaByKBs([]string{"kb"}); !errors.Is(err, failure) || got != nil {
		t.Fatal("engine error lost", got, err)
	}
}
