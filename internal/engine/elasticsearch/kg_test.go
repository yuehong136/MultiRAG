package elasticsearch

import (
	"context"
	"encoding/json"
	"fmt"
	"multirag/internal/engine/types"
	"net/http"
	"strings"
	"testing"
)

func TestKGSearchCanonicalExpressionsAndProjection(t *testing.T) {
	e := testEngine(t, func(w http.ResponseWriter, r *http.Request) {
		var body map[string]interface{}
		if json.NewDecoder(r.Body).Decode(&body) != nil {
			t.Error("body")
		}
		raw, _ := json.Marshal(body)
		for _, part := range []string{`"kb_id":["kb"]`, `"knowledge_graph_kwd":"entity"`, `"entity_kwd^10"`, `"q_2_vec"`} {
			if !strings.Contains(string(raw), part) {
				t.Error("missing", part, string(raw))
			}
		}
		fmt.Fprint(w, `{"hits":{"total":{"value":1},"hits":[{"_id":"physical","_source":{"id":"entity","rank_flt":0}}]}}`)
	})
	backing := []string{"entity_kwd", "rank_flt", "untouched"}
	result, err := e.Search(t.Context(), &types.SearchRequest{IndexNames: []string{"multirag_owned"}, KbIDs: []string{"kb"}, Filter: map[string]interface{}{"knowledge_graph_kwd": "entity"}, SelectFields: backing[:2], MatchExprs: []interface{}{&types.MatchTextExpr{Fields: []string{"entity_kwd^10"}, MatchingText: "Alpha"}, &types.MatchDenseExpr{VectorColumnName: "q_2_vec", EmbeddingData: []float64{.1, .2}, TopN: 7}, &types.FusionExpr{Method: "weighted_sum", FusionParams: map[string]interface{}{"weights": "0.05,0.95"}}}})
	if err != nil || len(result.Chunks) != 1 || fmt.Sprint(result.Chunks[0]["rank_flt"]) != "0" {
		t.Fatal(result, err)
	}
	if backing[2] != "untouched" {
		t.Fatal("projection mutated", backing)
	}
}
func TestKGCanonicalMetadataPrecisionErrorsAndScope(t *testing.T) {
	calls := 0
	e := testEngine(t, func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Query().Get("case") == "failure" {
			w.WriteHeader(503)
			return
		}
		fmt.Fprint(w, `{"hits":{"total":{"value":1},"hits":[{"_id":"physical","_source":{"id":"doc","meta_fields":{"exact":9007199254740993}}}]}}`)
	})
	result, err := e.Search(t.Context(), &types.SearchRequest{IndexNames: []string{"multirag_doc_meta_owned"}, KbIDs: []string{"kb"}, SelectFields: []string{"meta_fields"}})
	if err != nil || len(result.Chunks) != 1 {
		t.Fatal(result, err)
	}
	n, ok := result.Chunks[0]["meta_fields"].(map[string]interface{})["exact"].(json.Number)
	if !ok || n.String() != "9007199254740993" {
		t.Fatal("precision lost", result)
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if _, err := e.Search(ctx, &types.SearchRequest{IndexNames: []string{"owned"}}); err != context.Canceled {
		t.Fatal(err)
	}
	empty, err := e.Search(t.Context(), &types.SearchRequest{IndexNames: []string{"owned"}, KbIDs: []string{}})
	if err != nil || empty.Total != 0 || calls != 1 {
		t.Fatal(empty, err, calls)
	}
	bad := testEngine(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(503) })
	if _, err := bad.Search(t.Context(), &types.SearchRequest{IndexNames: []string{"owned"}, KbIDs: []string{"kb"}}); err == nil {
		t.Fatal("backend failure hidden")
	}
}
