package elasticsearch

import (
	"encoding/json"
	"fmt"
	"multirag/internal/engine/types"
	"net/http"
	"reflect"
	"strings"
	"testing"
)

func TestMetadataKeyDeletionKeepsCanonicalQueryAndLiteralKeys(t *testing.T) {
	calls := 0
	e := testEngine(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "HEAD" {
			w.WriteHeader(200)
			return
		}
		calls++
		var body map[string]interface{}
		if json.NewDecoder(r.Body).Decode(&body) != nil {
			t.Error("invalid body")
		}
		if strings.HasSuffix(r.URL.Path, "/_search") {
			fmt.Fprint(w, `{"hits":{"total":{"value":1},"hits":[{"_id":"physical","_source":{"id":"doc","kb_id":"kb","meta_fields":{"keep":1,"a.b'[]":2}}}]}}`)
			return
		}
		if !strings.HasSuffix(r.URL.Path, "/_update_by_query") || r.URL.Query().Get("refresh") != "true" {
			t.Error(r.URL)
		}
		script := body["script"].(map[string]interface{})
		params := script["params"].(map[string]interface{})
		if !reflect.DeepEqual(params["keys"], []interface{}{"a.b'[]"}) || strings.Contains(script["source"].(string), "a.b'[]") {
			t.Error("interpolated key", body)
		}
		fmt.Fprint(w, `{"updated":1,"failures":[]}`)
	})
	for _, keys := range [][]string{nil, {}, {""}, {"a", "a"}} {
		if err := e.DeleteMetadataKeys(t.Context(), "doc", "kb", keys, "tenant"); err == nil {
			t.Fatal("invalid keys")
		}
	}
	if calls != 0 {
		t.Fatal("invalid reached backend")
	}
	if err := e.DeleteMetadataKeys(t.Context(), "doc", "kb", []string{"a.b'[]"}, "tenant"); err != nil || calls != 2 {
		t.Fatal(calls, err)
	}
}
func TestSearchEmptyExplicitScopeDoesNotBroaden(t *testing.T) {
	e := testEngine(t, func(http.ResponseWriter, *http.Request) { t.Fatal("empty scope reached backend") })
	result, err := e.Search(t.Context(), &types.SearchRequest{IndexNames: []string{"owned"}, KbIDs: []string{}})
	if err != nil || result.Total != 0 || len(result.Chunks) != 0 {
		t.Fatal(result, err)
	}
}

func TestESResultConsumersHaveStableIDsCountsAndSafeHighlight(t *testing.T) {
	e := &elasticsearchEngine{}
	chunks := []map[string]interface{}{
		{"id": "logical", "_id": "physical", "tag_kwd": []interface{}{"two", "one"}, "content_with_weight": "<script>alert(1)</script> Alpha 中文"},
		{"_id": "fallback", "tag_kwd": "two###three", "highlight": map[string]interface{}{"content_with_weight": []interface{}{"<em>Alpha</em><img src=x onerror=bad>"}}},
	}
	if got := e.GetChunkIDs(chunks); !reflect.DeepEqual(got, []string{"logical", "fallback"}) {
		t.Fatal(got)
	}
	counts := e.GetAggregation(chunks, "tag_kwd")
	if len(counts) != 3 || counts[0]["key"] != "two" || counts[0]["count"] != 2 || counts[1]["key"] != "one" {
		t.Fatal(counts)
	}
	highlights := e.GetHighlight(chunks, []string{"Alpha", "中文", "", "Alpha"}, "content_with_weight")
	if !strings.Contains(highlights["logical"], "<em>Alpha</em>") || !strings.Contains(highlights["logical"], "<em>中文</em>") || strings.Contains(highlights["logical"], "<script>") || strings.Contains(highlights["fallback"], "<img") {
		t.Fatal(highlights)
	}
	if chunks[0]["content_with_weight"] != "<script>alert(1)</script> Alpha 中文" {
		t.Fatal("mutated caller")
	}
}
