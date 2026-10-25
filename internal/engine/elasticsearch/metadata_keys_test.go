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

func TestMetadataKeyDeletionKeepsPhysicalIDAndLiteralKeys(t *testing.T) {
	calls := 0
	e := testEngine(t, func(w http.ResponseWriter, r *http.Request) {
		calls++
		var body map[string]interface{}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		switch r.URL.Path {
		case "/multirag_doc_meta_tenant/_search":
			raw, _ := json.Marshal(body)
			if !strings.Contains(string(raw), `"id":"doc"`) || !strings.Contains(string(raw), `"kb_id":"kb"`) {
				t.Fatal("unscoped identity query", body)
			}
			fmt.Fprint(w, `{"hits":{"total":{"value":1},"hits":[{"_id":"python-physical","_source":{"id":"doc","kb_id":"kb"}}]}}`)
		case "/multirag_doc_meta_tenant/_update/python-physical":
			if r.URL.Query().Get("refresh") != "wait_for" {
				t.Fatal("missing refresh")
			}
			script := body["script"].(map[string]interface{})
			params := script["params"].(map[string]interface{})
			if !reflect.DeepEqual(params["keys"], []interface{}{"a.b'[]"}) || params["id"] != "doc" || params["kb"] != "kb" || strings.Contains(script["source"].(string), "a.b'[]") {
				t.Fatal("keys interpolated or lost scope", body)
			}
			fmt.Fprint(w, `{"result":"deleted"}`)
		default:
			t.Fatal(r.URL.Path)
		}
	})
	for _, keys := range [][]string{nil, {}, {""}, {"a", "a"}} {
		if err := e.DeleteMetadataKeys(t.Context(), "doc", "kb", keys, "tenant"); err == nil {
			t.Fatal("invalid keys accepted", keys)
		}
	}
	if calls != 0 {
		t.Fatal("invalid selection reached backend")
	}
	if err := e.DeleteMetadataKeys(t.Context(), "doc", "kb", []string{"a.b'[]"}, "tenant"); err != nil || calls != 2 {
		t.Fatal(calls, err)
	}
}

func TestMetadataDeletionRejectsDuplicateIdentityAndFalseAcknowledgment(t *testing.T) {
	for _, mode := range []string{"duplicate", "missing", "bad-result", "backend"} {
		t.Run(mode, func(t *testing.T) {
			writes := 0
			e := testEngine(t, func(w http.ResponseWriter, r *http.Request) {
				if strings.HasSuffix(r.URL.Path, "/_search") {
					count := 1
					hits := `{"_id":"one","_source":{"id":"doc","kb_id":"kb"}}`
					if mode == "duplicate" {
						count = 2
						hits += `,` + `{"_id":"two","_source":{"id":"doc","kb_id":"kb"}}`
					}
					if mode == "missing" {
						count = 0
						hits = ""
					}
					fmt.Fprintf(w, `{"hits":{"total":{"value":%d},"hits":[%s]}}`, count, hits)
					return
				}
				writes++
				if mode == "backend" {
					w.WriteHeader(503)
					fmt.Fprint(w, `{"error":"unavailable"}`)
					return
				}
				fmt.Fprint(w, `{"result":"created"}`)
			})
			if err := e.DeleteMetadataKeys(t.Context(), "doc", "kb", []string{"a"}, "tenant"); err == nil {
				t.Fatal("false success", mode)
			}
			if (mode == "duplicate" || mode == "missing") && writes != 0 {
				t.Fatal("ambiguous row mutated")
			}
		})
	}
}

func TestSearchEmptyExplicitScopeDoesNotBroaden(t *testing.T) {
	e := testEngine(t, func(http.ResponseWriter, *http.Request) { t.Fatal("empty scope reached backend") })
	result, err := e.Search(t.Context(), &types.SearchRequest{IndexNames: []string{"owned"}, KbIDs: []string{}})
	if err != nil || result.Total != 0 || len(result.Chunks) != 0 {
		t.Fatal(result, err)
	}
}

func TestFieldsPreserveIndexedArraysAndCallerInput(t *testing.T) {
	input := map[string]interface{}{"id": "chunk", "important_kwd": []string{"indexed"}, "question_kwd": []string{"question"}, "content": "source", "content_with_weight": "indexed content"}
	e := &elasticsearchEngine{}
	got := e.GetFields([]map[string]interface{}{input}, []string{"important_kwd", "question_kwd", "content_with_weight"})["chunk"]
	if len(got) != 3 || !reflect.DeepEqual(got["important_kwd"], []string{"indexed"}) || got["content_with_weight"] != "indexed content" {
		t.Fatal(got)
	}
	if len(input) != 5 || input["content_ltks"] != nil {
		t.Fatal("caller mutated", input)
	}
}

func TestESResultConsumersHaveStableIDsCountsAndSafeHighlight(t *testing.T) {
	e := &elasticsearchEngine{}
	chunks := []map[string]interface{}{
		{"id": "logical", "_id": "physical", "tag_kwd": []interface{}{"two", "one"}, "content_with_weight": "<script>alert(1)</script> Alpha 中文"},
		{"_id": "fallback", "tag_kwd": "two###three", "highlight": map[string]interface{}{"content_with_weight": []interface{}{"<em>Alpha</em><img src=x onerror=bad>"}}},
	}
	if got := e.GetDocIDs(chunks); !reflect.DeepEqual(got, []string{"logical", "fallback"}) {
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

func TestESDocumentNamesAggregateLiterally(t *testing.T) {
	e := &elasticsearchEngine{}
	chunks := []map[string]interface{}{
		{"docnm_kwd": "Report, Q1.pdf"},
		{"docnm_kwd": "Report, Q1.pdf"},
		{"docnm_kwd": " spaced name.txt "},
		{"docnm_kwd": []string{"array, name.txt"}},
	}
	expected := []map[string]interface{}{
		{"key": "Report, Q1.pdf", "count": 2},
		{"key": " spaced name.txt ", "count": 1},
		{"key": "array, name.txt", "count": 1},
	}
	if actual := e.GetAggregation(chunks, "docnm_kwd"); !reflect.DeepEqual(actual, expected) {
		t.Fatalf("document names changed during aggregation: %#v", actual)
	}
}
