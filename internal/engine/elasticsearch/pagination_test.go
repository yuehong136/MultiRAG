package elasticsearch

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"reflect"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"

	"multirag/internal/engine/types"
)

func TestSearchDeepUsesPITAndSearchAfterAtActualHTTPBoundary(t *testing.T) {
	var searches, closed atomic.Int64
	engine, journal := registeredKGEngine(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/multirag_owned/_pit" {
			fmt.Fprint(w, `{"id":"registered-search-pit"}`)
			return
		}
		var body map[string]interface{}
		decoder := json.NewDecoder(r.Body)
		decoder.UseNumber()
		if err := decoder.Decode(&body); err != nil {
			t.Error(err)
			w.WriteHeader(400)
			return
		}
		if r.Method == http.MethodDelete && r.URL.Path == "/_pit" {
			closed.Add(1)
			if body["id"] != "registered-search-pit-next" {
				t.Error("latest PIT not released", body)
			}
			fmt.Fprint(w, `{"succeeded":true}`)
			return
		}
		searches.Add(1)
		if r.URL.Path != "/_search" || body["from"] != nil || body["pit"].(map[string]interface{})["id"] == nil {
			t.Error("deep search lost PIT or retained from", r.URL.Path, body)
		}
		scope, _ := json.Marshal(body["query"])
		if !strings.Contains(string(scope), `"kb_id":["owned"]`) || !strings.Contains(string(scope), `"doc_id":["document"]`) {
			t.Error("scope lost", string(scope))
		}
		start := 0
		if cursor, ok := body["search_after"].([]interface{}); ok {
			number, err := strconv.Atoi(cursor[len(cursor)-1].(json.Number).String())
			if err != nil {
				t.Error(err)
			}
			start = number + 1
		}
		hits := make([]map[string]interface{}, 0, 1000)
		for i := start; i < min(start+1000, 10005); i++ {
			hits = append(hits, map[string]interface{}{"_id": fmt.Sprintf("physical-%d", i), "_source": map[string]interface{}{"id": fmt.Sprintf("logical-%d", i), "kb_id": "owned"}, "sort": []interface{}{i}})
		}
		_ = json.NewEncoder(w).Encode(map[string]interface{}{"pit_id": "registered-search-pit-next", "hits": map[string]interface{}{"total": map[string]interface{}{"value": 10005, "relation": "eq"}, "hits": hits}})
	})
	request := &types.SearchRequest{IndexNames: []string{"multirag_owned"}, KbIDs: []string{"owned"}, Filter: map[string]interface{}{"doc_id": []string{"document"}}, Offset: 10000, Limit: 5}
	recordKGSearchInput(t, journal, request)
	journal.Event("reserve-pit", map[string]interface{}{"identity": "registered-search-pit", "endpoint": "fixture listener", "scope": "multirag_owned/owned"})
	result, err := engine.Search(t.Context(), request)
	if err != nil || result.Total != 10005 || len(result.Chunks) != 5 {
		t.Fatal(result, err)
	}
	for i, row := range result.Chunks {
		if row["id"] != fmt.Sprintf("logical-%d", 10000+i) {
			t.Fatal(row)
		}
	}
	if searches.Load() != 11 || closed.Load() != 1 {
		t.Fatal("cursor count or cleanup mismatch", searches.Load(), closed.Load())
	}
	journal.Event("pit-cleanup-readback", map[string]interface{}{"closed": closed.Load() == 1})
}

func TestMetadataScannerExactCompleteSnapshotAndCallbackFailureRelease(t *testing.T) {
	sentinel := errors.New("visitor failed")
	for _, fail := range []bool{false, true} {
		t.Run(fmt.Sprint(fail), func(t *testing.T) {
			var closed atomic.Bool
			engine, journal := registeredKGEngine(t, func(w http.ResponseWriter, r *http.Request) {
				if strings.HasSuffix(r.URL.Path, "/_pit") && r.Method == http.MethodPost {
					fmt.Fprint(w, `{"id":"metadata-pit"}`)
					return
				}
				if r.Method == http.MethodDelete {
					closed.Store(true)
					fmt.Fprint(w, `{"succeeded":true}`)
					return
				}
				var body map[string]interface{}
				decoder := json.NewDecoder(r.Body)
				decoder.UseNumber()
				if err := decoder.Decode(&body); err != nil {
					t.Error(err)
				}
				scope, _ := json.Marshal(body["query"])
				if !strings.Contains(string(scope), `"kb_id":["owned"]`) || strings.Contains(string(scope), "available_int") {
					t.Error("metadata scope changed", body)
				}
				if !reflect.DeepEqual(body["sort"], []interface{}{map[string]interface{}{"_shard_doc": "asc"}}) {
					t.Error("unstable metadata cursor ordering", body["sort"])
				}
				if body["search_after"] == nil {
					fmt.Fprint(w, `{"hits":{"total":{"value":2,"relation":"eq"},"hits":[{"_id":"physical-a","_source":{"id":"a","kb_id":"owned","meta_fields":{"exact":9007199254740993,"flags":[false,0,""]}},"sort":[9007199254740993]}]}}`)
				} else {
					if got := body["search_after"].([]interface{})[0]; got != json.Number("9007199254740993") {
						t.Error("exact cursor changed", got)
					}
					fmt.Fprint(w, `{"hits":{"total":{"value":2,"relation":"eq"},"hits":[{"_id":"physical-b","_source":{"id":"b","kb_id":"owned","meta_fields":{"exact":9007199254740994}},"sort":[9007199254740994]}]}}`)
				}
			})
			request := &types.SearchRequest{IndexNames: []string{"multirag_doc_meta_owner"}, KbIDs: []string{"owned"}}
			recordKGSearchInput(t, journal, request)
			journal.Event("reserve-pit", map[string]interface{}{"identity": "metadata-pit", "scope": "owned"})
			count := 0
			err := engine.ScanMetadata(t.Context(), request, func(rows []map[string]interface{}) error {
				for _, row := range rows {
					exact, ok := row["meta_fields"].(map[string]interface{})["exact"].(json.Number)
					if !ok || exact.String() != fmt.Sprintf("%d", 9007199254740993+count) {
						t.Fatal("exact scalar rounded", row)
					}
					count++
				}
				if fail {
					return sentinel
				}
				return nil
			})
			if (fail && !errors.Is(err, sentinel)) || (!fail && (err != nil || count != 2)) {
				t.Fatal(count, err)
			}
			if !closed.Load() {
				t.Fatal("PIT survived callback exit")
			}
			journal.Event("pit-cleanup-readback", map[string]interface{}{"absent": closed.Load()})
		})
	}
}

func TestPITCancellationAndPartialFailuresReleaseCursor(t *testing.T) {
	for _, mode := range []string{"cancel", "partial", "timeout", "missing-cursor", "repeated-cursor", "close-failed", "open-malformed", "page-malformed", "page-missing-hits", "partial-close-failed"} {
		t.Run(mode, func(t *testing.T) {
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			var closed atomic.Bool
			engine, journal := registeredKGEngine(t, func(w http.ResponseWriter, r *http.Request) {
				if strings.HasSuffix(r.URL.Path, "/_pit") && r.Method == http.MethodPost {
					if mode == "open-malformed" {
						fmt.Fprint(w, `{"id":"pit"} trailing`)
					} else {
						fmt.Fprint(w, `{"id":"pit"}`)
					}
					return
				}
				if r.Method == http.MethodDelete {
					var body struct {
						ID string `json:"id"`
					}
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
					}
					expected := "pit"
					if mode == "partial" || mode == "timeout" || mode == "page-malformed" || mode == "page-missing-hits" || mode == "partial-close-failed" {
						expected = "rotated-pit"
					}
					if body.ID != expected {
						t.Errorf("released PIT %q, want latest %q", body.ID, expected)
					}
					closed.Store(true)
					if mode == "close-failed" || mode == "partial-close-failed" {
						w.WriteHeader(500)
					} else {
						fmt.Fprint(w, `{"succeeded":true}`)
					}
					return
				}
				switch mode {
				case "partial", "partial-close-failed":
					fmt.Fprint(w, `{"pit_id":"rotated-pit","_shards":{"failed":1},"hits":{"total":{"value":1},"hits":[]}}`)
				case "timeout":
					fmt.Fprint(w, `{"pit_id":"rotated-pit","timed_out":true,"hits":{"total":{"value":1},"hits":[]}}`)
				case "page-malformed":
					fmt.Fprint(w, `{"pit_id":"rotated-pit","hits":{"total":{"value":1},"hits":[]}} trailing`)
				case "page-missing-hits":
					fmt.Fprint(w, `{"pit_id":"rotated-pit","hits":{}}`)
				case "missing-cursor":
					fmt.Fprint(w, `{"hits":{"total":{"value":2},"hits":[{"_source":{"id":"a","kb_id":"owned"}}]}}`)
				case "repeated-cursor":
					fmt.Fprint(w, `{"hits":{"total":{"value":100},"hits":[{"_source":{"id":"a","kb_id":"owned"},"sort":[1]}]}}`)
				default:
					fmt.Fprint(w, `{"hits":{"total":{"value":1},"hits":[{"_source":{"id":"a","kb_id":"owned"},"sort":[1]}]}}`)
				}
			})
			journal.Event("reserve-pit", map[string]interface{}{"identity": "pit", "mode": mode})
			request := &types.SearchRequest{IndexNames: []string{"multirag_doc_meta_owner"}, KbIDs: []string{"owned"}}
			recordKGSearchInput(t, journal, request)
			err := engine.ScanMetadata(ctx, request, func(rows []map[string]interface{}) error {
				if mode == "cancel" {
					cancel()
					return ctx.Err()
				}
				return nil
			})
			if err == nil || !closed.Load() {
				t.Fatal("failure lost or cursor survived", err, closed.Load())
			}
			if mode == "partial-close-failed" && (!strings.Contains(err.Error(), "incomplete search") || !strings.Contains(err.Error(), "HTTP 500")) {
				t.Fatal("business failure or cleanup failure was lost", err)
			}
			if mode == "cancel" && !errors.Is(err, context.Canceled) {
				t.Fatal("cancellation identity lost", err)
			}
			journal.Event("pit-cleanup-readback", map[string]interface{}{"deleteObserved": closed.Load(), "acknowledged": mode != "close-failed" && mode != "partial-close-failed"})
		})
	}
}

func TestGraphVisibilityRequiresInternalKindAtHTTPBoundary(t *testing.T) {
	var seen []string
	engine, journal := registeredKGEngine(t, func(w http.ResponseWriter, r *http.Request) {
		var body map[string]interface{}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		raw, _ := json.Marshal(body["query"])
		seen = append(seen, string(raw))
		fmt.Fprint(w, `{"hits":{"total":{"value":0},"hits":[]}}`)
	})
	request := &types.SearchRequest{IndexNames: []string{"multirag_owner"}, KbIDs: []string{"owned"}, Filter: map[string]interface{}{"knowledge_graph_kwd": "entity", "available_int": 0}}
	for _, kind := range []types.SearchKind{types.SearchKindDefault, types.SearchKindKnowledgeGraph} {
		request.Kind = kind
		recordKGSearchInput(t, journal, request)
		journal.Event("internal-search-kind", map[string]interface{}{"kind": kind})
		if _, err := engine.Search(t.Context(), request); err != nil {
			t.Fatal(err)
		}
	}
	if !strings.Contains(seen[0], "must_not") || strings.Contains(seen[1], "must_not") || !strings.Contains(seen[1], `"available_int":0`) {
		t.Fatal("graph visibility broadened ordinary search or lost internal zero", seen)
	}
}

func TestIncompleteSearchResponseFailsAtActualHTTPBoundary(t *testing.T) {
	for _, raw := range []string{`{}`, `{"hits":{"hits":[]}}`, `{"hits":{"total":{"value":0}}}`, `{"hits":{"total":{"value":-1},"hits":[]}}`, `{"hits":{"total":{"value":10000,"relation":"gte"},"hits":[]}}`, `{"timed_out":true,"hits":{"total":{"value":0},"hits":[]}}`, `{"_shards":{"failed":1},"hits":{"total":{"value":0},"hits":[]}}`} {
		t.Run(raw, func(t *testing.T) {
			engine, journal := registeredKGEngine(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, raw) })
			request := &types.SearchRequest{IndexNames: []string{"multirag_owned"}, KbIDs: []string{"owned"}}
			recordKGSearchInput(t, journal, request)
			if result, err := engine.Search(t.Context(), request); err == nil || result != nil {
				t.Fatal("partial or malformed response reported success", result, err)
			}
		})
	}
}

func TestMetadataScannerCrossesResultWindowOnOneSnapshot(t *testing.T) {
	const total = 10005
	var searches atomic.Int64
	var active atomic.Bool
	engine, journal := registeredKGEngine(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/_pit") && r.Method == http.MethodPost {
			active.Store(true)
			fmt.Fprint(w, `{"id":"large-metadata-pit"}`)
			return
		}
		if r.Method == http.MethodDelete {
			active.Store(false)
			fmt.Fprint(w, `{"succeeded":true}`)
			return
		}
		searches.Add(1)
		var body map[string]interface{}
		decoder := json.NewDecoder(r.Body)
		decoder.UseNumber()
		if err := decoder.Decode(&body); err != nil {
			t.Error(err)
		}
		if !active.Load() || r.URL.Path != "/_search" || body["from"] != nil || body["pit"].(map[string]interface{})["id"] != "large-metadata-pit" {
			t.Error("metadata left its registered snapshot", body)
		}
		start := 0
		if cursor, ok := body["search_after"].([]interface{}); ok {
			number, err := strconv.Atoi(cursor[0].(json.Number).String())
			if err != nil {
				t.Error(err)
			}
			start = number + 1
		}
		hits := make([]map[string]interface{}, 0, 1000)
		for i := start; i < min(start+1000, total); i++ {
			hits = append(hits, map[string]interface{}{"_id": fmt.Sprintf("physical-%d", i), "_source": map[string]interface{}{"id": fmt.Sprintf("doc-%d", i), "kb_id": "owned", "meta_fields": map[string]interface{}{"ordinal": i, "exact": json.Number("9007199254740993"), "flag": false}}, "sort": []interface{}{i}})
		}
		_ = json.NewEncoder(w).Encode(map[string]interface{}{"hits": map[string]interface{}{"total": map[string]interface{}{"value": total, "relation": "eq"}, "hits": hits}})
	})
	request := &types.SearchRequest{IndexNames: []string{"multirag_doc_meta_owner"}, KbIDs: []string{"owned"}}
	recordKGSearchInput(t, journal, request)
	journal.Event("reserve-pit", map[string]interface{}{"identity": "large-metadata-pit", "scope": "owned", "rows": total})
	count := 0
	err := engine.ScanMetadata(t.Context(), request, func(rows []map[string]interface{}) error {
		for _, row := range rows {
			fields := row["meta_fields"].(map[string]interface{})
			if row["id"] != fmt.Sprintf("doc-%d", count) || fields["ordinal"] != json.Number(strconv.Itoa(count)) || fields["exact"] != json.Number("9007199254740993") || fields["flag"] != false {
				t.Fatal("late metadata row lost or rounded", row, count)
			}
			count++
		}
		return nil
	})
	if err != nil || count != total || searches.Load() != 11 || active.Load() {
		t.Fatal("truncated snapshot or PIT survived", count, searches.Load(), active.Load(), err)
	}
	journal.Event("pit-cleanup-readback", map[string]interface{}{"absent": !active.Load(), "observedRows": count})
}

func TestSearchAndMetadataPITKeepLiteralIndexEscapes(t *testing.T) {
	var shallow, opened, closed bool
	engine, journal := registeredKGEngine(t, func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodDelete:
			closed = true
			fmt.Fprint(w, `{"succeeded":true}`)
		case strings.HasSuffix(r.URL.Path, "/_pit"):
			opened = true
			if r.URL.EscapedPath() != "/multirag_doc_meta_owner%252A/_pit" {
				t.Error("literal metadata index became wildcard", r.URL.EscapedPath())
			}
			fmt.Fprint(w, `{"id":"literal-index-pit"}`)
		default:
			if r.URL.Path != "/_search" {
				shallow = true
				if r.URL.EscapedPath() != "/multirag_owner%252A/_search" {
					t.Error("literal index became wildcard", r.URL.EscapedPath())
				}
			}
			fmt.Fprint(w, `{"hits":{"total":{"value":0},"hits":[]}}`)
		}
	})
	request := &types.SearchRequest{IndexNames: []string{"multirag_owner%2A"}, KbIDs: []string{"owned"}}
	recordKGSearchInput(t, journal, request)
	if _, err := engine.Search(t.Context(), request); err != nil {
		t.Fatal(err)
	}
	request.IndexNames = []string{"multirag_doc_meta_owner%2A"}
	recordKGSearchInput(t, journal, request)
	journal.Event("reserve-pit", map[string]interface{}{"identity": "literal-index-pit", "scope": "owned"})
	if err := engine.ScanMetadata(t.Context(), request, func(rows []map[string]interface{}) error { return nil }); err != nil {
		t.Fatal(err)
	}
	if !shallow || !opened || !closed {
		t.Fatal("missing scoped boundary or cleanup", shallow, opened, closed)
	}
	journal.Event("pit-cleanup-readback", map[string]interface{}{"closed": closed})
}

func TestPITOpenRejectsFailedShardsAndClosesIdentity(t *testing.T) {
	var searches, closed atomic.Int64
	engine, journal := registeredKGEngine(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/_pit") && r.Method == http.MethodPost {
			if r.URL.Query().Get("allow_partial_search_results") != "" {
				t.Error("ES8.11 PIT-open does not support allow_partial_search_results")
			}
			fmt.Fprint(w, `{"id":"partial-open-pit","_shards":{"failed":1}}`)
			return
		}
		if r.Method == http.MethodDelete {
			closed.Add(1)
			fmt.Fprint(w, `{"succeeded":true}`)
			return
		}
		searches.Add(1)
		fmt.Fprint(w, `{"hits":{"total":{"value":0},"hits":[]}}`)
	})
	request := &types.SearchRequest{IndexNames: []string{"multirag_doc_meta_owner"}, KbIDs: []string{"owned"}}
	recordKGSearchInput(t, journal, request)
	journal.Event("reserve-pit", map[string]interface{}{"identity": "partial-open-pit", "scope": "owned"})
	err := engine.ScanMetadata(t.Context(), request, func([]map[string]interface{}) error { t.Error("partial PIT returned successful metadata"); return nil })
	if err == nil || !strings.Contains(err.Error(), "incomplete PIT") || searches.Load() != 0 || closed.Load() != 1 {
		t.Fatal(err, searches.Load(), closed.Load())
	}
	journal.Event("pit-cleanup-readback", map[string]interface{}{"closed": closed.Load() == 1})
}
