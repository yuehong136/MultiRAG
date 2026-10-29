package elasticsearch

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"net"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sync/atomic"
	"testing"
	"time"

	es "github.com/elastic/go-elasticsearch/v8"

	"multirag/internal/engine/types"
	"multirag/internal/testsupport"
)

func registeredKGEngine(t *testing.T, handler http.HandlerFunc) (*elasticsearchEngine, *testsupport.CaseJournal) {
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
		journal.Event("reserve-listener", map[string]any{"network": "tcp", "address": address})
		var err error
		listener, err = net.Listen("tcp", address)
		if err == nil {
			break
		}
		journal.Event("listener-bind-failed", map[string]any{"address": address})
	}
	if listener == nil {
		t.Fatal("unable to bind a registered KG fixture listener")
	}
	server := &httptest.Server{Listener: listener, Config: &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Elastic-Product", "Elasticsearch")
		handler(w, r)
	})}}
	t.Cleanup(func() {
		server.Close()
		connection, err := net.DialTimeout("tcp", address, 200*time.Millisecond)
		if err == nil {
			connection.Close()
			t.Error("KG fixture listener survived cleanup")
		}
		journal.Event("listener-cleanup-readback", map[string]any{"address": address, "absent": err != nil})
	})
	server.Start()
	client, err := es.NewClient(es.Config{Addresses: []string{server.URL}, DisableRetry: true})
	if err != nil {
		t.Fatal(err)
	}
	return &elasticsearchEngine{client: client}, journal
}

func recordKGSearchInput(t *testing.T, journal *testsupport.CaseJournal, request *types.SearchRequest) {
	t.Helper()
	raw, err := json.Marshal(request)
	if err != nil {
		// Preserve the exact bits of deliberately invalid rank numbers instead
		// of hashing pointer addresses from a diagnostic representation.
		copyRequest := *request
		copyRequest.RankFeature = nil
		bits := make(map[string]uint64, len(request.RankFeature))
		for key, value := range request.RankFeature {
			bits[key] = math.Float64bits(value)
		}
		raw, err = json.Marshal(struct {
			Request         *types.SearchRequest
			RankFeatureBits map[string]uint64
		}{&copyRequest, bits})
		if err != nil {
			t.Fatal(err)
		}
	}
	hash := sha256.Sum256(raw)
	journal.Event("search-input", map[string]any{"sha256": hex.EncodeToString(hash[:])})
}

func TestKGSearchConsumesFieldsFusionRankingAndScope(t *testing.T) {
	var calls atomic.Int64
	var journal *testsupport.CaseJournal
	engine, journal := registeredKGEngine(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/multirag_owned/_search" {
			t.Error("unexpected index path", r.URL.Path)
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			w.WriteHeader(400)
			return
		}
		query := body["query"].(map[string]any)["bool"].(map[string]any)
		text := query["must"].(map[string]any)["query_string"].(map[string]any)
		if !reflect.DeepEqual(text["fields"], []any{"entity_kwd^10", "content_ltks^2"}) || query["boost"] != 0.05 {
			t.Error("entity fields or fusion text weight lost", query)
		}
		knn := body["knn"].(map[string]any)
		if knn["boost"] != 0.95 || knn["similarity"] != 0.3 {
			t.Error("dense weight or similarity lost", knn)
		}
		knnFilter := knn["filter"].(map[string]any)["bool"].(map[string]any)
		if _, lexical := knnFilter["must"]; lexical {
			t.Error("explicit fusion incorrectly prefilters dense candidates by text")
		}
		if !reflect.DeepEqual(knnFilter["filter"], query["filter"]) {
			t.Error("dense branch lost scalar scope")
		}
		if !reflect.DeepEqual(body["_source"], []any{"entity_kwd", "rank_flt", "q_2_vec"}) {
			t.Error("KG projection lost", body["_source"])
		}
		features := query["should"].([]any)
		if len(features) != 2 || features[0].(map[string]any)["rank_feature"].(map[string]any)["field"] != "pagerank_fea" || features[1].(map[string]any)["rank_feature"].(map[string]any)["field"] != "tag_feas.topic" {
			t.Error("rank features lost", features)
		}
		raw, _ := json.Marshal(query["filter"])
		journal.Event("observed-filter", map[string]any{"json": string(raw)})
		var expected any
		_ = json.Unmarshal([]byte(`[{"terms":{"kb_id":["kb-owned"]}},{"bool":{"must_not":[{"range":{"available_int":{"lt":1}}}]}},{"bool":{"filter":[{"term":{"knowledge_graph_kwd":"entity"}}]}}]`), &expected)
		if !reflect.DeepEqual(query["filter"], expected) {
			t.Error("KG or KB scope lost", query["filter"])
		}
		fmt.Fprint(w, `{"hits":{"total":{"value":1},"hits":[{"_id":"physical","_score":0.9,"_source":{"entity_kwd":["Alpha"],"rank_flt":0}}]}}`)
	})
	backing := []string{"entity_kwd", "rank_flt", "caller-owned-guard"}
	fields := backing[:2]
	request := &types.SearchRequest{
		IndexNames: []string{"multirag_owned"}, KbIDs: []string{"kb-owned"}, SelectFields: fields, Limit: 7,
		Filter: map[string]any{"knowledge_graph_kwd": "entity"},
		MatchExprs: []any{
			&types.MatchDenseExpr{VectorColumnName: "q_2_vec", EmbeddingData: []float64{0.1, 0.2}, TopN: 7, ExtraOptions: map[string]any{"similarity": 0.3}},
			&types.MatchTextExpr{Fields: []string{"entity_kwd^10", "content_ltks^2"}, MatchingText: "Alpha"},
			&types.FusionExpr{Method: "weighted_sum", FusionParams: map[string]any{"weights": "0.05,0.95"}},
		},
		RankFeature: map[string]float64{"topic": 0, "pagerank_fea": 10},
	}
	recordKGSearchInput(t, journal, request)
	result, err := engine.Search(t.Context(), request)
	if err != nil || len(result.Chunks) != 1 || result.Chunks[0]["rank_flt"] != float64(0) {
		t.Fatal(result, err)
	}
	if !reflect.DeepEqual(backing, []string{"entity_kwd", "rank_flt", "caller-owned-guard"}) || calls.Load() != 1 {
		t.Fatal("caller projection mutated or unexpected requests", backing, calls.Load())
	}
}

func TestKGCommunitySortAndLegacySearchDefaults(t *testing.T) {
	var calls atomic.Int64
	engine, journal := registeredKGEngine(t, func(w http.ResponseWriter, r *http.Request) {
		call := calls.Add(1)
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if call == 1 {
			expected := []any{map[string]any{"weight_flt": map[string]any{"order": "desc", "unmapped_type": "float"}}}
			if !reflect.DeepEqual(body["sort"], expected) {
				t.Error("community ordering lost", body["sort"])
			}
		} else {
			query := body["query"].(map[string]any)["bool"].(map[string]any)
			if query["boost"] != 0.3 {
				t.Error("legacy text boost changed", query)
			}
			fields := query["must"].(map[string]any)["query_string"].(map[string]any)["fields"]
			if !reflect.DeepEqual(fields, []any{"title_tks^10", "title_sm_tks^5", "important_kwd^30", "important_tks^20", "question_tks^20", "content_ltks^2", "content_sm_ltks"}) {
				t.Error("legacy text fields changed", fields)
			}
			if _, ok := body["knn"].(map[string]any)["boost"]; ok {
				t.Error("legacy dense boost changed")
			}
		}
		fmt.Fprint(w, `{"hits":{"total":{"value":0},"hits":[]}}`)
	})
	request := &types.SearchRequest{IndexNames: []string{"multirag_owned"}, KbIDs: []string{"kb-owned"}, Filter: map[string]any{"knowledge_graph_kwd": "community_report"}, OrderBy: (&types.OrderByExpr{}).Desc("weight_flt")}
	recordKGSearchInput(t, journal, request)
	if _, err := engine.Search(t.Context(), request); err != nil {
		t.Fatal(err)
	}
	request.OrderBy = nil
	request.MatchExprs = []any{&types.MatchTextExpr{MatchingText: "Alpha"}, &types.MatchDenseExpr{VectorColumnName: "q_2_vec", EmbeddingData: []float64{0.1, 0.2}}}
	recordKGSearchInput(t, journal, request)
	if _, err := engine.Search(t.Context(), request); err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 2 {
		t.Fatal("unexpected requests", calls.Load())
	}
}

func TestKGInvalidOptionsEmptyScopeAndCancellationIssueNoRequests(t *testing.T) {
	var calls atomic.Int64
	engine, journal := registeredKGEngine(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		fmt.Fprint(w, `{"hits":{"total":{"value":0},"hits":[]}}`)
	})
	for _, weights := range []string{"0,0", "NaN,1", "1,+Inf", "-1,1", "1", "a,b"} {
		request := &types.SearchRequest{IndexNames: []string{"multirag_owned"}, KbIDs: []string{"kb-owned"}, MatchExprs: []any{&types.MatchTextExpr{MatchingText: "Alpha"}, &types.MatchDenseExpr{}, &types.FusionExpr{Method: "weighted_sum", FusionParams: map[string]any{"weights": weights}}}}
		recordKGSearchInput(t, journal, request)
		if _, err := engine.Search(t.Context(), request); err == nil {
			t.Error("invalid fusion accepted", weights)
		}
	}
	for _, request := range []*types.SearchRequest{
		{IndexNames: []string{"multirag_owned"}, KbIDs: []string{"kb-owned"}, RankFeature: map[string]float64{"pagerank_fea": math.NaN()}},
		{IndexNames: []string{"multirag_owned"}, KbIDs: []string{"kb-owned"}, RankFeature: map[string]float64{"topic": -1}},
		{IndexNames: []string{"multirag_owned"}, KbIDs: []string{"kb-owned"}, OrderBy: (&types.OrderByExpr{}).Desc("weight_flt;bad")},
		{IndexNames: []string{"multirag_owned"}, KbIDs: []string{"kb-owned"}, OrderBy: &types.OrderByExpr{Fields: []types.OrderByField{{Field: "weight_flt", Type: types.OrderByType(9)}}}},
		{IndexNames: []string{"multirag_*"}, KbIDs: []string{"kb-owned"}},
	} {
		recordKGSearchInput(t, journal, request)
		if _, err := engine.Search(t.Context(), request); err == nil {
			t.Error("invalid request accepted", request)
		}
	}
	empty := &types.SearchRequest{IndexNames: []string{"multirag_owned"}, KbIDs: []string{}}
	recordKGSearchInput(t, journal, empty)
	if result, err := engine.Search(t.Context(), empty); err != nil || len(result.Chunks) != 0 {
		t.Fatal(result, err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	request := &types.SearchRequest{IndexNames: []string{"multirag_owned"}, KbIDs: []string{"kb-owned"}}
	recordKGSearchInput(t, journal, request)
	if _, err := engine.Search(ctx, request); err == nil {
		t.Fatal("cancelled query succeeded")
	}
	if calls.Load() != 0 {
		t.Fatal("forbidden request issued", calls.Load())
	}
}

func TestKGEngineMetadataOnlyResponsesPreserveLargeIntegers(t *testing.T) {
	var calls atomic.Int64
	engine, journal := registeredKGEngine(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		fmt.Fprint(w, `{"hits":{"total":{"value":1},"hits":[{"_id":"physical","_score":0.5,"_source":{"id":"owned","meta_fields":{"exact":9007199254740993},"rank_flt":0.25}}]}}`)
	})
	for _, test := range []struct {
		name          string
		indices       []string
		preserveExact bool
	}{
		{"metadata", []string{"multirag_doc_meta_owned"}, true},
		{"multiple_metadata", []string{"multirag_doc_meta_owned", "multirag_doc_meta_other"}, true},
		{"ordinary", []string{"multirag_owned"}, false},
		{"mixed", []string{"multirag_doc_meta_owned", "multirag_owned"}, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			request := &types.SearchRequest{IndexNames: test.indices, KbIDs: []string{"kb-owned"}}
			recordKGSearchInput(t, journal, request)
			result, err := engine.Search(t.Context(), request)
			if err != nil || len(result.Chunks) != 1 {
				t.Fatal(result, err)
			}
			chunk := result.Chunks[0]
			value := chunk["meta_fields"].(map[string]any)["exact"]
			if test.preserveExact {
				if exact, ok := value.(json.Number); !ok || exact.String() != "9007199254740993" {
					t.Fatalf("metadata integer lost precision: %T %v", value, value)
				}
			} else if number, ok := value.(float64); !ok || number != 9007199254740992 {
				t.Fatalf("ordinary or mixed source numeric type changed: %T %v", value, value)
			}
			if chunk["_score"] != float64(0.5) {
				t.Fatal("typed score representation changed", chunk["_score"])
			}
		})
	}
	if calls.Load() != 4 {
		t.Fatal("unexpected requests", calls.Load())
	}
}
