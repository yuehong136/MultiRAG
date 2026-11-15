package elasticsearch

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"math"
	"net"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"

	es "github.com/elastic/go-elasticsearch/v8"
	"multirag/internal/testsupport"
)

func testEngine(t *testing.T, handler http.HandlerFunc) *elasticsearchEngine {
	t.Helper()
	journal := testsupport.NewCase(t)
	var listener net.Listener
	var address string
	for attempt := 0; attempt < 30; attempt++ {
		var value [2]byte
		if _, err := rand.Read(value[:]); err != nil {
			t.Fatal(err)
		}
		address = fmt.Sprintf("127.0.0.1:%d", 30000+int(binary.BigEndian.Uint16(value[:]))%20000)
		journal.Event("reserve-ES-listener", map[string]any{"address": address})
		var err error
		listener, err = net.Listen("tcp", address)
		if err == nil {
			break
		}
		journal.Event("ES-listener-bind-failed", map[string]any{"address": address})
	}
	if listener == nil {
		t.Fatal("owned ES listener unavailable")
	}
	srv := &httptest.Server{Listener: listener, Config: &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Elastic-Product", "Elasticsearch")
		handler(w, r)
	})}}
	srv.Start()
	t.Cleanup(func() {
		srv.Close()
		connection, err := net.DialTimeout("tcp", address, 200*time.Millisecond)
		if err == nil {
			connection.Close()
			t.Error("owned ES listener survived cleanup")
			return
		}
		journal.Event("ES-listener-cleaned", map[string]any{"address": address})
	})
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
	for _, part := range []string{`"kb_id":"kb-a"`, `"id":["chunk-a","chunk-b"]`, `"must_not"`, `"pp_source_doc_ids":["doc-a"]`} {
		if !strings.Contains(string(raw), part) {
			t.Fatalf("missing %s: %s", part, raw)
		}
	}
	if _, ok := condition["kb_id"]; ok {
		t.Fatal("condition mutated")
	}
	if !reflect.DeepEqual(value, map[string]interface{}{"available_int": 0, "source_doc_ids": []string{"doc-a"}}) {
		t.Fatal("value mutated")
	}
	for _, bad := range []interface{}{nil, []string{}, []byte("chunk"), struct{}{}, math.NaN()} {
		if err := e.UpdateChunks(t.Context(), map[string]interface{}{"id": bad}, value, "multirag_tenant", "kb-a"); err == nil {
			t.Fatalf("accepted %T", bad)
		}
	}
	if len(bodies) != 1 {
		t.Fatal("invalid conditions reached backend")
	}
}
func TestBulkStableIdentityAndPartialFailure(t *testing.T) {
	var identities []string
	e := testEngine(t, func(w http.ResponseWriter, r *http.Request) {
		decoder := json.NewDecoder(r.Body)
		var action, doc map[string]interface{}
		if decoder.Decode(&action) != nil || decoder.Decode(&doc) != nil {
			t.Error("invalid bulk body")
			return
		}
		identities = append(identities, action["index"].(map[string]interface{})["_id"].(string))
		fmt.Fprint(w, `{"errors":true,"items":[{"index":{"status":400,"error":{"type":"mapper_parsing_exception"}}}]}`)
	})
	for _, kb := range []string{"kb-a", "kb-a", "kb-b"} {
		failed, err := e.InsertChunks(t.Context(), []map[string]interface{}{{"id": "same", "doc_id": "doc", "kb_id": kb}}, "multirag_tenant", kb)
		if err == nil || !reflect.DeepEqual(failed, []string{"same"}) {
			t.Fatal(failed, err)
		}
	}
	if identities[0] != identities[1] || identities[0] == identities[2] {
		t.Fatal("cross-KB physical collision", identities)
	}
	if _, err := e.InsertChunks(t.Context(), []map[string]interface{}{{"id": "same", "doc_id": "doc", "kb_id": "other"}}, "multirag_tenant", "kb-a"); err == nil {
		t.Fatal("foreign chunk accepted")
	}
	if len(identities) != 3 {
		t.Fatal("foreign chunk issued request")
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
			t.Fatal("partial failure", reply)
		}
	}
	e := testEngine(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(403) })
	if _, err := e.DeleteChunks(t.Context(), map[string]interface{}{"id": "x"}, "multirag_tenant", "kb"); err == nil {
		t.Fatal("forbidden became missing")
	}
	for _, condition := range []map[string]interface{}{{"id": []string{}}, {"x": struct{}{}}, {"must_not": map[string]interface{}{"unsupported": "x"}}} {
		if _, err := e.DeleteMetadata(t.Context(), condition, "tenant"); err == nil {
			t.Fatal("invalid metadata delete accepted")
		}
	}
}
func TestMetadataCanonicalQueryKeepsLiteralIdentityAndCompletion(t *testing.T) {
	for _, valid := range []bool{true, false} {
		t.Run(fmt.Sprint(valid), func(t *testing.T) {
			writes := 0
			e := testEngine(t, func(w http.ResponseWriter, r *http.Request) {
				if r.Method == "HEAD" {
					w.WriteHeader(200)
					return
				}
				writes++
				if !strings.HasSuffix(r.URL.Path, "/_update_by_query") || r.URL.Query().Get("refresh") != "true" {
					t.Error(r.URL)
				}
				var body map[string]interface{}
				if json.NewDecoder(r.Body).Decode(&body) != nil {
					t.Error("invalid body")
				}
				raw, _ := json.Marshal(body["query"])
				if !strings.Contains(string(raw), `"id":"O'Brien"`) || !strings.Contains(string(raw), `"kb_id":"kb-a"`) {
					t.Error("literal or scope lost", string(raw))
				}
				if valid {
					fmt.Fprint(w, `{"updated":1,"failures":[]}`)
				} else {
					fmt.Fprint(w, `{"updated":1,"failures":[{"reason":"partial"}]}`)
				}
			})
			err := e.UpdateMetadata(t.Context(), "O'Brien", "kb-a", map[string]interface{}{"zero": 0}, "tenant")
			if (err == nil) != valid || writes != 1 {
				t.Fatal(writes, err)
			}
		})
	}
}

func TestExclusiveSkillStoreDropRequiresAcknowledgement(t *testing.T) {
	for _, reply := range []string{`{"acknowledged":true}`, `{"acknowledged":false}`, `{}`, `broken`} {
		t.Run(reply, func(t *testing.T) {
			e := testEngine(t, func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/skill_owned" {
					t.Error("exclusive skill scope changed", r.URL.Path)
				}
				if r.Method == http.MethodHead {
					w.WriteHeader(200)
					return
				}
				if r.Method != http.MethodDelete {
					t.Error("skill store was not deleted", r.Method)
				}
				fmt.Fprint(w, reply)
			})
			err := e.DropChunkStore(t.Context(), "skill_owned", "skill")
			if (err == nil) != (reply == `{"acknowledged":true}`) {
				t.Fatal("unacknowledged skill deletion reported success", err)
			}
		})
	}
	// Invalid names must fail before touching even an uninitialized ES client.
	e := &elasticsearchEngine{}
	for _, name := range []string{"multirag_tenant", "skill_*", "skill_one,skill_two", "skill_", "_all"} {
		if err := e.DropChunkStore(t.Context(), name, "skill"); err == nil {
			t.Fatal("unbounded skill index scope accepted", name)
		}
	}
}
