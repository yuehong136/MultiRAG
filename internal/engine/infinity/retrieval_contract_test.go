package infinity

import (
	"encoding/json"
	"math"
	"reflect"
	"strings"
	"testing"

	"multirag/internal/engine/types"
)

func TestInfinityRetrievalRejectsInvalidScopeBeforeSDKAccess(t *testing.T) {
	store := &infinityEngine{}
	for _, request := range []*types.SearchRequest{
		{IndexNames: []string{"multirag_owned"}, KbIDs: []string{"owned"}, Filter: map[string]interface{}{"doc_id": []string{}}},
		{IndexNames: []string{"multirag_owned"}, KbIDs: []string{"owned"}, Filter: map[string]interface{}{"available_int": math.NaN()}},
		{IndexNames: []string{"multirag_owned"}, KbIDs: []string{"owned"}, Kind: types.SearchKindKnowledgeGraph, Filter: map[string]interface{}{"knowledge_graph_kwd": "entity"}},
		{IndexNames: []string{"multirag_owned"}, KbIDs: []string{"owned"}, Kind: types.SearchKindKnowledgeGraph, Filter: map[string]interface{}{"knowledge_graph_kwd": []string{"entity"}, "available_int": 0}},
	} {
		result, err := store.Search(t.Context(), request)
		if err == nil || result != nil || strings.Contains(err.Error(), "client not initialized") {
			t.Fatal("invalid retrieval reached SDK boundary", result, err)
		}
	}
	result, err := store.Search(t.Context(), &types.SearchRequest{IndexNames: []string{"multirag_owned"}, KbIDs: []string{}})
	if err != nil || len(result.Chunks) != 0 || result.Total != 0 {
		t.Fatal("explicit empty scope reached SDK", result, err)
	}
}

func TestInfinityTotalRejectsMalformedAndPreservesExactCount(t *testing.T) {
	for _, test := range []struct {
		raw  string
		rows int
		want int64
	}{
		{`{"total_hits_count":0}`, 0, 0}, {`{"total_hits_count":12}`, 7, 12},
		{`{"total_hits_count":9007199254740993}`, 3, 9007199254740993},
	} {
		got, err := infinitySearchTotal(test.raw, test.rows)
		if err != nil || got != test.want {
			t.Fatal(test, got, err)
		}
	}
	for _, raw := range []string{"", `{}`, `{"total_hits_count":null}`, `{"total_hits_count":false}`, `{"total_hits_count":1.5}`, `{"total_hits_count":-1}`, `{"total_hits_count":9223372036854775808}`, `{"total_hits_count":1} {}`, `{"total_hits_count":0}`} {
		if _, err := infinitySearchTotal(raw, 1); err == nil {
			t.Fatal("invalid or incomplete total accepted", raw)
		}
	}
}

func TestInfinityTiedScoresKeepSourceOrder(t *testing.T) {
	rows := []map[string]interface{}{{"id": "first", "_score": float64(0)}, {"id": "second", "_score": float64(0)}, {"id": "winner", "_score": float64(1)}, {"id": "third", "_score": float64(0)}}
	got := sortByScore(rows, 4)
	ids := []string{}
	for _, row := range got {
		ids = append(ids, row["id"].(string))
	}
	if !reflect.DeepEqual(ids, []string{"winner", "first", "second", "third"}) {
		t.Fatal(ids)
	}
	// Exact metadata numbers remain a JSON number at the matcher boundary.
	fields, err := decodeMetadataFields(`{"legal_zero":0,"flag":false,"large":9007199254740993}`)
	if err != nil || fields["large"] != json.Number("9007199254740993") || fields["legal_zero"] != json.Number("0") || fields["flag"] != false {
		t.Fatal(fields, err)
	}
}
