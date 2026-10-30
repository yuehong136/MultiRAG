package elasticsearch

import (
	"fmt"
	"net/http"
	"reflect"
	"strings"
	"testing"

	"multirag/internal/engine/types"
)

func TestAfter10bbeESDecodeAndResultConsumers(t *testing.T) {
	e := testEngine(t, func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"hits":{"total":{"value":2},"hits":[{"_id":"physical","_source":{"id":"logical","kb_id":"kb","docnm_kwd":"Report, Q1.pdf","content_with_weight":"Unmatched sentence. Alpha 中文 tail."},"highlight":{"title_tks":["wrong-field"],"content_with_weight":["<em>Alpha</em> <script>unsafe</script>"]}},{"_id":"fallback","_source":null}]}}`)
	})
	result, err := e.Search(t.Context(), &types.SearchRequest{IndexNames: []string{"owned"}, KbIDs: []string{"kb"}, Limit: 2})
	if err != nil || len(result.Chunks) != 2 {
		t.Fatal(result, err)
	}
	if ids := e.GetDocIDs(result.Chunks); !reflect.DeepEqual(ids, []string{"logical", "fallback"}) {
		t.Fatal(ids)
	}
	fields := e.GetFields(result.Chunks, []string{"id", "docnm_kwd"})
	if fields["fallback"]["id"] != "fallback" || fields["logical"]["id"] != "logical" || len(fields["fallback"]) != 1 {
		t.Fatal(fields)
	}
	if _, exists := result.Chunks[1]["id"]; exists {
		t.Fatal("GetFields mutated input")
	}
	highlighted := e.GetHighlight(result.Chunks, []string{"Alpha", "中文"}, "content_with_weight")
	if strings.Contains(highlighted["logical"], "wrong-field") || strings.Contains(highlighted["logical"], "<script>") || !strings.Contains(highlighted["logical"], "<em>Alpha</em>") {
		t.Fatal(highlighted)
	}
	plain := []map[string]interface{}{{"id": "source", "content_with_weight": "Unmatched sentence. Concatenate cat 中文 tail."}}
	got := e.GetHighlight(plain, []string{"cat", "中文"}, "content_with_weight")["source"]
	// Full-field literal highlighting deliberately follows the later unified
	// highlighter, rather than the target's obsolete English/sentence heuristic.
	if !strings.Contains(got, "Unmatched sentence.") || !strings.Contains(got, "Con<em>cat</em>enate") || !strings.Contains(got, "<em>中文</em>") {
		t.Fatal(got)
	}
}
