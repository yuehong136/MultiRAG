package service

import (
	"encoding/json"
	"errors"
	"math"
	"reflect"
	"testing"
)

func TestSavedRetrievalConfigPreservesPresenceAndNumericID(t *testing.T) {
	one := 1.0
	yes := true
	n := 1024
	id := int64(1)
	original := &RetrievalTestRequest{DatasetIDs: []string{"kb"}, Question: "q", SimilarityThreshold: &one, VectorSimilarityWeight: &one, Keyword: &yes, UseKG: &yes, TopK: &n, CrossLanguages: []string{"en"}, DocIDs: []string{"doc"}, TenantRerankID: &id}
	config := map[string]interface{}{"similarity_threshold": json.Number("0"), "vector_similarity_weight": json.Number("0"), "keyword": false, "use_kg": false, "top_k": json.Number("0"), "cross_languages": []string{}, "doc_ids": []string{}, "tenant_rerank_id": json.Number("9007199254740993")}
	got, err := savedRetrievalRequest(original, config)
	if err != nil {
		t.Fatal(err)
	}
	if *got.SimilarityThreshold != 0 || *got.VectorSimilarityWeight != 0 || *got.Keyword || *got.UseKG || *got.TopK != 0 || got.DocIDs == nil || len(got.DocIDs) != 0 || got.CrossLanguages == nil || len(got.CrossLanguages) != 0 || *got.TenantRerankID != 9007199254740993 {
		t.Fatalf("presence lost: %#v", got)
	}
	if *original.SimilarityThreshold != 1 || *original.Keyword != true || !reflect.DeepEqual(original.DocIDs, []string{"doc"}) || *original.TenantRerankID != 1 {
		t.Fatal("request mutated")
	}
}

func TestSavedRetrievalConfigRejectsMalformedTypedSettings(t *testing.T) {
	for _, config := range []map[string]interface{}{
		{"similarity_threshold": nil}, {"similarity_threshold": -1}, {"vector_similarity_weight": 2}, {"top_k": -1}, {"top_k": 1.5}, {"keyword": "false"}, {"use_kg": 1}, {"cross_languages": []interface{}{1}}, {"tenant_rerank_id": "1"}, {"tenant_rerank_id": 0}, {"doc_ids": []interface{}{false}},
	} {
		if _, err := savedRetrievalRequest(&RetrievalTestRequest{Question: "q", DatasetIDs: []string{"kb"}}, config); !errors.Is(err, ErrRetrievalArgument) {
			t.Fatalf("%v accepted: %v", config, err)
		}
	}
}

func TestRetrievalRequestRejectsInvalidNumbers(t *testing.T) {
	for _, value := range []float64{math.NaN(), math.Inf(1), math.Inf(-1), -0.01, 1.01} {
		req := &RetrievalTestRequest{Question: "q", DatasetIDs: []string{"kb"}, VectorSimilarityWeight: &value}
		if !errors.Is(validateRetrievalRequest(req), ErrRetrievalArgument) {
			t.Fatalf("accepted %v", value)
		}
	}
	for _, value := range []float64{0, 1} {
		req := &RetrievalTestRequest{Question: "q", DatasetIDs: []string{"kb"}, VectorSimilarityWeight: &value}
		if err := validateRetrievalRequest(req); err != nil {
			t.Fatal(err)
		}
	}
	var req RetrievalTestRequest
	if err := json.Unmarshal([]byte(`{"dataset_ids":["kb"],"question":"q","tenant_rerank_id":9007199254740993}`), &req); err != nil || *req.TenantRerankID != 9007199254740993 {
		t.Fatalf("numeric ID: %v", err)
	}
	if err := json.Unmarshal([]byte(`{"tenant_rerank_id":"1"}`), &req); err == nil {
		t.Fatal("string model identity accepted")
	}
}

func TestThinkBlocksRemoveAllHiddenReasoning(t *testing.T) {
	for _, tt := range []struct{ in, want string }{{"plain", "plain"}, {"<think>a\nb</think>answer", "answer"}, {"prefix<think>a</think>partial<think>b</think>final", "final"}, {"<think>unclosed", "<think>unclosed"}} {
		if got := thinkBlockRE.ReplaceAllString(tt.in, ""); got != tt.want {
			t.Fatalf("%q -> %q want %q", tt.in, got, tt.want)
		}
	}
}

func TestRetrievalJSONPreservesMetadataNumber(t *testing.T) {
	var req RetrievalTestRequest
	raw := []byte(`{"question":"q","dataset_ids":["kb"],"meta_data_filter":{"manual":[{"value":9007199254740993}]}}`)
	if err := json.Unmarshal(raw, &req); err != nil {
		t.Fatal(err)
	}
	value := req.Filter["manual"].([]interface{})[0].(map[string]interface{})["value"]
	if value != json.Number("9007199254740993") {
		t.Fatalf("operand rounded: %T %v", value, value)
	}
	got, err := savedRetrievalRequest(&req, map[string]interface{}{"meta_data_filter": req.Filter})
	if err != nil || got.Filter["manual"].([]interface{})[0].(map[string]interface{})["value"] != value {
		t.Fatal("saved operand rounded", got, err)
	}
}

func TestRetrievalJSONRetainsKnownWebFieldsAndRejectsTypos(t *testing.T) {
	var req RetrievalTestRequest
	if err := json.Unmarshal([]byte(`{"question":"q","dataset_ids":["kb"],"highlight":true,"search_mode":{"type":"dense"},"reference_metadata":{"fields":[]},"metadata_condition":{}}`), &req); err != nil || req.Highlight == nil || !*req.Highlight {
		t.Fatal(err)
	}
	if err := json.Unmarshal([]byte(`{"question":"q","dataset_ids":["kb"],"keywrod":true}`), &req); err == nil {
		t.Fatal("unknown flag accepted")
	}
}
