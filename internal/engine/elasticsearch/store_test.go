package elasticsearch

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	es "github.com/elastic/go-elasticsearch/v8"
	"multirag/internal/engine/types"
)

func testEngine(t *testing.T, handler http.HandlerFunc) *elasticsearchEngine {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Elastic-Product", "Elasticsearch")
		handler(w, r)
	}))
	t.Cleanup(srv.Close)
	client, err := es.NewClient(es.Config{Addresses: []string{srv.URL}, DisableRetry: true})
	if err != nil {
		t.Fatal(err)
	}
	return &elasticsearchEngine{client: client}
}
func TestChunkWritesKeepTypedIDAndDatasetScope(t *testing.T) {
	var bodies []map[string]interface{}
	e := testEngine(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "HEAD" {
			w.WriteHeader(200)
			return
		}
		if r.URL.Path != "/multirag_tenant/_update_by_query" || r.URL.Query().Get("refresh") != "true" {
			t.Error(r.URL)
		}
		var body map[string]interface{}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		bodies = append(bodies, body)
		fmt.Fprint(w, `{"updated":1,"version_conflicts":0,"failures":[]}`)
	})
	condition := map[string]interface{}{"id": []string{"chunk-a", "chunk-b"}, "must_not": map[string]interface{}{"exists": "compile_kwd"}}
	value := map[string]interface{}{"available_int": 0, "source_doc_ids": []string{"doc-a"}}
	if err := e.UpdateChunks(t.Context(), condition, value, "multirag_tenant", "kb-a"); err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(bodies[0])
	for _, part := range []string{`"kb_id":"kb-a"`, `"id":["chunk-a","chunk-b"]`, `"must_not"`, `"fields":{"available_int":0,"source_doc_ids":["doc-a"]}`} {
		if !strings.Contains(string(raw), part) {
			t.Fatalf("missing %s: %s", part, raw)
		}
	}
	if !reflect.DeepEqual(value, map[string]interface{}{"available_int": 0, "source_doc_ids": []string{"doc-a"}}) {
		t.Fatal("caller value mutated")
	}
	for _, bad := range []interface{}{nil, []string{}, []byte("chunk"), struct{}{}, math.NaN()} {
		if err := e.UpdateChunks(t.Context(), map[string]interface{}{"id": bad}, value, "multirag_tenant", "kb-a"); err == nil {
			t.Fatalf("accepted %T", bad)
		}
	}
	if len(bodies) != 1 {
		t.Fatal("invalid update made a request")
	}
}
func TestSearchAndGetChunkConsumeFiltersAndSelectFields(t *testing.T) {
	e := testEngine(t, func(w http.ResponseWriter, r *http.Request) {
		var body map[string]interface{}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		raw, _ := json.Marshal(body)
		for _, part := range []string{`"kb_id":["kb-a"]`, `"id":"parent"`, `"_source":["content_with_weight"]`} {
			if !strings.Contains(string(raw), part) {
				t.Errorf("missing %s: %s", part, raw)
			}
		}
		fmt.Fprint(w, `{"hits":{"total":{"value":1},"hits":[{"_id":"physical","_source":{"id":"parent","content_with_weight":"only parent"}}]}}`)
	})
	req := &types.SearchRequest{IndexNames: []string{"multirag_tenant"}, KbIDs: []string{"kb-a"}, Filter: map[string]interface{}{"id": "parent"}, SelectFields: []string{"content_with_weight"}}
	if result, err := e.Search(t.Context(), req); err != nil || len(result.Chunks) != 1 {
		t.Fatal(result, err)
	}
}
func TestBulkStableIdentityAndPartialFailure(t *testing.T) {
	var identities []string
	e := testEngine(t, func(w http.ResponseWriter, r *http.Request) {
		decoder := json.NewDecoder(r.Body)
		var action, doc map[string]interface{}
		if err := decoder.Decode(&action); err != nil {
			t.Error(err)
		}
		if err := decoder.Decode(&doc); err != nil {
			t.Error(err)
		}
		identities = append(identities, action["index"].(map[string]interface{})["_id"].(string))
		fmt.Fprint(w, `{"errors":true,"items":[{"index":{"status":400,"error":{"type":"mapper_parsing_exception"}}}]}`)
	})
	for _, kb := range []string{"kb-a", "kb-a", "kb-b"} {
		failed, err := e.InsertChunks(t.Context(), []map[string]interface{}{{"id": "same", "kb_id": kb}}, "multirag_tenant", kb)
		if err == nil || !reflect.DeepEqual(failed, []string{"same"}) {
			t.Fatal(failed, err)
		}
	}
	if identities[0] != identities[1] || identities[0] == identities[2] {
		t.Fatal("unstable or cross-KB identity", identities)
	}
	if _, err := e.InsertChunks(t.Context(), []map[string]interface{}{{"id": "same", "kb_id": "kb-b"}}, "multirag_tenant", "kb-a"); err == nil {
		t.Fatal("foreign KB insert accepted")
	}
	if len(identities) != 3 {
		t.Fatal("foreign insert issued request")
	}
}
func TestQueryWritesAndMissingStoreDoNotHideErrors(t *testing.T) {
	for _, reply := range []string{`{"deleted":2,"failures":[{"reason":"disk full"}]}`, `{"deleted":2,"version_conflicts":1}`, `{"deleted":2,"timed_out":true}`, `{}`} {
		e := testEngine(t, func(w http.ResponseWriter, r *http.Request) {
			if r.Method == "HEAD" {
				w.WriteHeader(200)
				return
			}
			fmt.Fprint(w, reply)
		})
		if _, err := e.DeleteChunks(context.Background(), map[string]interface{}{"id": []string{"a"}}, "multirag_tenant", "kb"); err == nil {
			t.Fatal("partial failure accepted", reply)
		}
	}
	e := testEngine(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(403) })
	if _, err := e.DeleteChunks(t.Context(), map[string]interface{}{}, "multirag_tenant", "kb"); err == nil {
		t.Fatal("forbidden became missing index")
	}
	for _, condition := range []map[string]interface{}{{"id": []string{}}, {"x": struct{}{}}, {"must_not": map[string]interface{}{"unsupported": "x"}}} {
		if _, err := e.DeleteMetadata(t.Context(), condition, "tenant"); err == nil {
			t.Fatal("invalid condition accepted")
		}
	}
}

func TestMetadataKeepsPythonPhysicalIdentityAndRejectsDuplicates(t *testing.T) {
	for _, duplicate := range []bool{false, true} {
		t.Run(fmt.Sprint(duplicate), func(t *testing.T) {
			writes := 0
			e := testEngine(t, func(w http.ResponseWriter, r *http.Request) {
				if strings.HasSuffix(r.URL.Path, "/_search") {
					hits := `{"_id":"python-physical-id","_source":{"id":"O'Brien","kb_id":"kb-a"}}`
					if duplicate {
						fmt.Fprintf(w, `{"hits":{"total":{"value":2},"hits":[%s,%s]}}`, hits, hits)
					} else {
						fmt.Fprintf(w, `{"hits":{"total":{"value":1},"hits":[%s]}}`, hits)
					}
					return
				}
				writes++
				if !strings.HasSuffix(r.URL.Path, "/_update/python-physical-id") {
					t.Error("legacy row would be duplicated", r.URL.Path)
				}
				var body map[string]interface{}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				params := body["script"].(map[string]interface{})["params"].(map[string]interface{})
				if params["id"] != "O'Brien" || params["kb"] != "kb-a" {
					t.Error("literal identity changed", params)
				}
				fmt.Fprint(w, `{"result":"updated"}`)
			})
			err := e.UpdateMetadata(t.Context(), "O'Brien", "kb-a", map[string]interface{}{"zero": 0}, "tenant")
			if duplicate {
				if err == nil || writes != 0 {
					t.Fatal("duplicate metadata mutated", writes, err)
				}
			} else if err != nil || writes != 1 {
				t.Fatal(writes, err)
			}
		})
	}
}
