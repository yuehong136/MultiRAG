package elasticsearch

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"

	"multirag/internal/engine/types"
)

func TestGetChunkKeepsScopeWithoutRetrievalAvailabilityDefault(t *testing.T) {
	bodies := []string{}
	e := testEngine(t, func(w http.ResponseWriter, request *http.Request) {
		var body map[string]any
		if err := json.NewDecoder(request.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		raw, _ := json.Marshal(body)
		bodies = append(bodies, string(raw))
		if strings.Contains(string(raw), "available_int") {
			fmt.Fprint(w, `{"hits":{"total":{"value":0},"hits":[]}}`)
			return
		}
		fmt.Fprint(w, `{"hits":{"total":{"value":1},"hits":[{"_id":"physical","_source":{"id":"c","doc_id":"d","kb_id":"kb","available_int":0}}]}}`)
	})
	for _, scope := range [][]string{nil, {""}, {"kb", ""}, {"kb", "kb"}} {
		row, err := e.GetChunk(t.Context(), "tenant", "c", scope)
		if len(scope) == 0 {
			if row != nil || err != nil {
				t.Fatal(row, err)
			}
		} else if err == nil {
			t.Fatal("invalid scope accepted", scope)
		}
	}
	if len(bodies) != 0 {
		t.Fatal("invalid scopes made requests")
	}
	row, err := e.GetChunkForManagement(t.Context(), "tenant", "c", []string{"kb"})
	if err != nil || row.(map[string]any)["available_int"] != float64(0) {
		t.Fatal(row, err)
	}
	if !strings.Contains(bodies[0], `"kb_id":["kb"]`) || !strings.Contains(bodies[0], `"id":"c"`) || strings.Contains(bodies[0], "available_int") {
		t.Fatal("scoped administrative read gained a retrieval filter", bodies[0])
	}
	if row, err := e.GetChunk(t.Context(), "tenant", "c", []string{"kb"}); err != nil || row != nil {
		t.Fatal("ordinary GetChunk broadened retrieval visibility", row, err)
	}
	if !strings.Contains(bodies[1], `"available_int"`) {
		t.Fatal("ordinary GetChunk lost its availability policy", bodies[1])
	}
	if _, err := e.Search(t.Context(), &types.SearchRequest{IndexNames: []string{"tenant"}, KbIDs: []string{"kb"}, MatchExprs: []any{&types.MatchTextExpr{MatchingText: "body"}}}); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(bodies[2], `"available_int"`) {
		t.Fatal("retrieval availability default changed", bodies[2])
	}
}
