package service

import (
	"encoding/json"
	"errors"
	"multirag/internal/engine/types"
	"reflect"
	"testing"
)

func TestDocumentMetadataScopeSQLAndCanonicalEngine(t *testing.T) {
	f := canonicalLink(t)
	if err := f.service.convertFiles([]string{f.one}, []string{f.a}, f.owner); err != nil {
		t.Fatal(err)
	}
	doc := canonicalLinkedDocs(t, f, f.a)[0]
	index := &canonicalMetadataEngine{response: nil}
	s := f.service.documentSvc
	s.metadataSvc = &MetadataService{docEngine: index}
	index.response = &types.SearchMetadataResult{MetadataRecords: []map[string]interface{}{{"id": doc.ID, "kb_id": f.a, "meta_fields": `{"large":9007199254740993,"zero":0,"enabled":false,"empty":""}`}}}
	values, err := s.GetDocumentMetadataByID(doc.ID)
	if err != nil || values["large"] != json.Number("9007199254740993") || values["zero"] != json.Number("0") || values["enabled"] != false || values["empty"] != "" {
		t.Fatal(values, err)
	}
	if index.request.TenantID != f.owner || !reflect.DeepEqual(index.request.Filter["id"], []string{doc.ID}) || index.request.Filter["kb_id"] != f.a || index.request.Limit != 1 {
		t.Fatal("metadata lookup lost SQL-resolved owner and exact document scope", index.request)
	}
	failure := errors.New("controlled metadata failure")
	index.failure = failure
	if values, err := s.GetDocumentMetadataByID(doc.ID); values != nil || !errors.Is(err, failure) {
		t.Fatal("metadata provider error became SQL fallback/empty success", values, err)
	}
	index.failure = nil
	index.response.MetadataRecords[0]["meta_fields"] = "{broken"
	if values, err := s.GetDocumentMetadataByID(doc.ID); err == nil || values != nil {
		t.Fatal("corrupt metadata accepted", values, err)
	}
}
