package types

import (
	"encoding/json"
	"math"
	"testing"
)

func TestRerankWindowKeepsCompletePages(t *testing.T) {
	for _, size := range []int{1, 5, 7, 10, 30, 50, 64, 100, math.MaxInt} {
		for _, top := range []int{0, 5, 30, 50, 55, 64, 100, 1024, math.MaxInt} {
			window := RerankWindow(size, top)
			if window < 1 || (size > 1 && window%size != 0) {
				t.Fatalf("size=%d top=%d window=%d", size, top, window)
			}
		}
	}
	if got := RerankWindow(10, 1024); got != 70 {
		t.Fatal(got)
	}
}

func TestInternalGraphVisibilityIsScopedAndCannotDecodeFromJSON(t *testing.T) {
	request := SearchRequest{IndexNames: []string{"multirag_owner"}, KbIDs: []string{"owned"}, Filter: map[string]interface{}{"knowledge_graph_kwd": "entity", "available_int": 0}, Kind: SearchKindKnowledgeGraph}
	if allowed, err := request.ExplicitVisibility(); err != nil || !allowed {
		t.Fatal(allowed, err)
	}
	request.Kind = SearchKindDefault
	if allowed, err := request.ExplicitVisibility(); err != nil || allowed {
		t.Fatal("client filter gained disabled visibility", allowed, err)
	}
	if err := json.Unmarshal([]byte(`{"Kind":1}`), &request); err != nil {
		t.Fatal(err)
	}
	if request.Kind != SearchKindDefault {
		t.Fatal("JSON set internal search kind", request.Kind)
	}
	for _, mutate := range []func(*SearchRequest){
		func(r *SearchRequest) { r.KbIDs = nil }, func(r *SearchRequest) { r.KbIDs = []string{"owned", "foreign"} },
		func(r *SearchRequest) { r.IndexNames = nil }, func(r *SearchRequest) { r.IncludeUnavailable = true },
		func(r *SearchRequest) { r.Filter["knowledge_graph_kwd"] = []string{"entity"} },
		func(r *SearchRequest) { r.Filter["knowledge_graph_kwd"] = "unknown" },
		func(r *SearchRequest) { r.Filter["available_int"] = 1 }, func(r *SearchRequest) { r.Filter["available_int"] = math.NaN() },
	} {
		copyRequest := SearchRequest{IndexNames: []string{"multirag_owner"}, KbIDs: []string{"owned"}, Filter: map[string]interface{}{"knowledge_graph_kwd": "entity", "available_int": 0}, Kind: SearchKindKnowledgeGraph}
		mutate(&copyRequest)
		if allowed, err := copyRequest.ExplicitVisibility(); err == nil || allowed {
			t.Fatal("invalid graph request accepted", copyRequest)
		}
	}
}
