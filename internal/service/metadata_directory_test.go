package service

import (
	"encoding/binary"
	"encoding/json"
	"reflect"
	"testing"
)

func TestMetadataDirectoryExactScalarAndEncodedNumbers(t *testing.T) {
	for _, value := range []interface{}{int64(9007199254740993), uint64(9007199254740993), json.Number("9007199254740993")} {
		if text, ok := metadataValueText(value); !ok || text != "9007199254740993" {
			t.Fatalf("integer precision lost: %T %q", value, text)
		}
	}
	raw := `{"large":9007199254740993,"boolean":false,"zero":0,"empty":""}`
	packed := make([]byte, 4)
	binary.LittleEndian.PutUint32(packed, uint32(len(raw)))
	packed = append(packed, []byte(raw)...)
	for _, value := range []interface{}{raw, []byte(raw), packed} {
		fields, err := ExtractMetaFields(map[string]interface{}{"meta_fields": value})
		if err != nil || fields["large"] != json.Number("9007199254740993") || fields["boolean"] != false || fields["empty"] != "" || fields["zero"] != json.Number("0") {
			t.Fatalf("encoded metadata precision/scalars lost: %T %#v %v", value, fields, err)
		}
		strict, err := decodeDocumentMetadata(value)
		if err != nil || !reflect.DeepEqual(strict, fields) {
			t.Fatalf("reference and directory decoders disagree: %T %#v %v", value, strict, err)
		}
	}
}

func TestMetadataReferencePublicEmptySelectionNeedsNoDependencies(t *testing.T) {
	svc := &MetadataService{}
	chunks := []map[string]interface{}{{"doc_id": "doc", "kb_id": "kb", "document_metadata": map[string]interface{}{"stale": true}}}
	got, err := svc.EnrichChunksWithDocMetadata(t.Context(), chunks, "actor", []string{})
	if err != nil || got[0]["document_metadata"] != nil || chunks[0]["document_metadata"] == nil {
		t.Fatal("empty selection required dependencies or mutated input", got, err)
	}
	if got, err := svc.EnrichChunksWithDocMetadata(t.Context(), nil, "actor", nil); err != nil || got != nil {
		t.Fatal("empty chunks required dependencies", got, err)
	}
}

func TestMetadataReferenceCopyPreservesNullValues(t *testing.T) {
	values := map[string]interface{}{"list": []interface{}(nil), "object": map[string]interface{}(nil), "empty": []interface{}{}}
	chunks := []map[string]interface{}{{"kb_id": "kb", "doc_id": "doc"}}
	got := AttachDocMetaToChunks(chunks, DocMetaMap{"kb": {"doc": values}}, nil)
	raw, err := json.Marshal(got[0]["document_metadata"])
	if err != nil || string(raw) != `{"empty":[],"list":null,"object":null}` {
		t.Fatal("copy changed null or empty values", string(raw), err)
	}
}

func TestMetadataDatasetFlattenAcceptsExactNumbers(t *testing.T) {
	for _, value := range []interface{}{json.Number("9007199254740993"), int64(9007199254740993), []interface{}{json.Number("0"), false, nil}, []string{"a", "b"}} {
		if values, err := flattenedMetadataValues(value); err != nil || len(values) == 0 {
			t.Fatal("valid typed scalar or array rejected", value, values, err)
		}
	}
	if values, err := flattenedMetadataValues(json.Number("9007199254740993")); err != nil || !reflect.DeepEqual(values, []string{"9007199254740993"}) {
		t.Fatal("large integer was rounded", values, err)
	}
	if _, err := flattenedMetadataValues(json.Number("NaN")); err == nil {
		t.Fatal("invalid numeric metadata accepted")
	}
}
