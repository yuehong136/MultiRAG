package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"

	"multirag/internal/engine"
	"multirag/internal/engine/elasticsearch"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/server"
	"multirag/internal/testsupport"
)

type kgReader struct {
	calls  int
	deny   string
	cancel context.CancelFunc
}

func (r *kgReader) GetReadableByID(ctx context.Context, id, user string) (*entity.Knowledgebase, error) {
	r.calls++
	if r.cancel != nil {
		r.cancel()
	}
	if id == r.deny || user != "owner" {
		return nil, errors.New("forbidden")
	}
	tenant := "a"
	if id == "kb-b" {
		tenant = "b"
	}
	return &entity.Knowledgebase{ID: id, TenantID: tenant}, ctx.Err()
}

type kgStore struct {
	engine.DocEngine
	calls     int
	err       error
	cancel    context.CancelFunc
	nilResult bool
}

func (s *kgStore) Search(ctx context.Context, r *types.SearchRequest) (*types.SearchResult, error) {
	s.calls++
	if s.cancel != nil {
		s.cancel()
	}
	if s.nilResult {
		return nil, nil
	}
	return &types.SearchResult{}, s.err
}

func TestKGSearchFailClosed(t *testing.T) {
	store := &kgStore{}
	reader := &kgReader{}
	svc := &KGSearchService{store: store, kbReader: reader}
	for _, ids := range [][]string{nil, {}, {""}} {
		rows, err := svc.SearchKGEntities(t.Context(), "owner", ids, "q", nil, 2)
		if len(ids) > 0 {
			if err == nil {
				t.Fatal("empty ID accepted")
			}
		} else if err != nil || len(rows) != 0 {
			t.Fatal(rows, err)
		}
	}
	if store.calls != 0 {
		t.Fatal("empty scope reached engine")
	}
	for _, limit := range []int{-1, 10001} {
		if _, err := svc.SearchKGEntities(t.Context(), "owner", []string{"kb-a"}, "q", nil, limit); err == nil {
			t.Fatal("invalid limit accepted")
		}
	}
	reader.deny = "kb-b"
	if _, err := svc.SearchKGEntities(t.Context(), "owner", []string{"kb-a", "kb-b"}, "q", nil, 2); err == nil || store.calls != 0 {
		t.Fatal("partial authorization queried", err, store.calls)
	}
	reader.deny = ""
	for _, actor := range []string{"", "foreign"} {
		if _, err := svc.SearchKGEntities(t.Context(), actor, []string{"kb-a"}, "q", nil, 2); err == nil || store.calls != 0 {
			t.Fatal(actor, err)
		}
	}
	if _, err := svc.SearchKGEntities(t.Context(), "owner", []string{"kb-a"}, "q", nil, 0); err != nil || store.calls != 0 {
		t.Fatal("zero limit broadened", err)
	}
	if _, err := svc.SearchKGEntitiesByTypes(t.Context(), "owner", []string{"kb-a"}, []string{}, 2); err != nil || store.calls != 0 {
		t.Fatal("empty types broadened", err)
	}
	if _, err := svc.SearchKGCommunityReports(t.Context(), "owner", []string{"kb-a"}, nil, 2); err != nil || store.calls != 0 {
		t.Fatal("empty entities broadened", err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	before := reader.calls
	if _, err := svc.SearchKGEntities(ctx, "owner", []string{"kb-a"}, "q", nil, 2); !errors.Is(err, context.Canceled) || reader.calls != before || store.calls != 0 {
		t.Fatal("cancelled work executed", err)
	}
	for _, mode := range []string{"nil", "error", "cancel"} {
		t.Run(mode, func(t *testing.T) {
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			store.nilResult = mode == "nil"
			store.err = nil
			store.cancel = nil
			if mode == "error" {
				store.err = errors.New("engine-failure")
			}
			if mode == "cancel" {
				store.cancel = cancel
			}
			if rows, err := svc.SearchKGEntities(ctx, "owner", []string{"kb-a"}, "q", nil, 2); err == nil || rows != nil {
				t.Fatal("bad engine result accepted", rows, err)
			}
		})
	}
}

func TestKGSearchRealElasticsearchHTTP(t *testing.T) {
	journal := testsupport.NewCase(t)
	journal.Event("register-controlled-engine-scope", map[string]any{"indices": []string{"multirag_a", "multirag_b"}, "datasets": []string{"kb-a", "kb-b"}, "mode": "HTTP fixture; no remote storage created"})
	var calls atomic.Int64
	fixture := registeredModelServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Elastic-Product", "Elasticsearch")
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/" {
			fmt.Fprint(w, `{"version":{"number":"8.19.1"}}`)
			return
		}
		calls.Add(1)
		var req map[string]any
		if json.NewDecoder(r.Body).Decode(&req) != nil {
			t.Error("invalid request")
			w.WriteHeader(400)
			return
		}
		raw, _ := json.Marshal(req)
		body := string(raw)
		id := "kb-a"
		if r.URL.Path == "/multirag_b/_search" {
			id = "kb-b"
		} else if r.URL.Path != "/multirag_a/_search" {
			t.Error("index scope", r.URL.Path)
		}
		if !strings.Contains(body, `"kb_id":["`+id+`"]`) {
			t.Error("KB scope", body)
		}
		other := "kb-b"
		if id == other {
			other = "kb-a"
		}
		if strings.Contains(body, other) {
			t.Error("cross tenant KB request", body)
		}
		for _, field := range []string{"_source", "knowledge_graph_kwd"} {
			if !strings.Contains(body, field) {
				t.Error("missing projection/filter", body)
			}
		}
		journal.Event("controlled-engine-request", map[string]any{"path": r.URL.Path, "body": req})
		source := map[string]any{}
		switch {
		case strings.Contains(body, `"community_report"`):
			if !strings.Contains(body, `"weight_flt":{"order":"desc","unmapped_type":"float"}`) {
				t.Error("community sort ignored", body)
			}
			source = map[string]any{"docnm_kwd": "report", "content_with_weight": "community", "weight_flt": 0.0, "entities_kwd": "A"}
		case strings.Contains(body, `"ty2ents"`):
			source = map[string]any{"content_with_weight": `{"person":["A"]}`}
		case strings.Contains(body, `"relation"`):
			source = map[string]any{"from_entity_kwd": []string{"A"}, "to_entity_kwd": []string{"B"}, "weight_int": 0, "content_with_weight": "relation"}
		default:
			if !strings.Contains(body, "entity_type_kwd") {
				t.Error("entity projection", body)
			}
			if !strings.Contains(body, "entity_type_kwd\":") && !strings.Contains(body, "entity_kwd^10") {
				t.Error("KG text fields ignored", body)
			}
			source = map[string]any{"entity_kwd": []string{"A"}, "entity_type_kwd": "person", "rank_flt": 0.0, "content_with_weight": "entity", "n_hop_with_weight": "[]"}
		}
		json.NewEncoder(w).Encode(map[string]any{"hits": map[string]any{"total": map[string]any{"value": 1}, "hits": []any{map[string]any{"_id": "kg", "_score": 0.0, "_source": source}}}})
	}))
	store, err := elasticsearch.NewEngine(&server.ElasticsearchConfig{Hosts: fixture.URL})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { store.Close() })
	svc := &KGSearchService{store: store, kbReader: &kgReader{}}
	ids := []string{"kb-a", "kb-b"}
	ents, err := svc.SearchKGEntities(t.Context(), "owner", ids, "A", nil, 1)
	if err != nil || len(ents) != 1 || ents[0].PageRank != 0 || ents[0].NhopJSON != "[]" {
		t.Fatal(ents, err)
	}
	byType, err := svc.SearchKGEntitiesByTypes(t.Context(), "owner", ids, []string{"person"}, 2)
	if err != nil || len(byType) != 2 {
		t.Fatal(byType, err)
	}
	rels, err := svc.SearchKGRelations(t.Context(), "owner", ids, "A", nil, 2)
	if err != nil || len(rels) != 2 || rels[0].From != "A" || rels[0].Weight != 0 {
		t.Fatal(rels, err)
	}
	reports, err := svc.SearchKGCommunityReports(t.Context(), "owner", ids, []string{"A"}, 1)
	if err != nil || len(reports) != 1 || reports[0].Content != "community" {
		t.Fatal(reports, err)
	}
	samples, err := svc.SearchKGTypeSamples(t.Context(), "owner", ids)
	if err != nil || !reflect.DeepEqual(samples["person"], []string{"A", "A"}) {
		t.Fatal(samples, err)
	}
	if calls.Load() != 10 {
		t.Fatal("query count", calls.Load())
	}
	journal.Event("controlled-engine-result-readback", map[string]any{"requests": calls.Load(), "entity_count": len(ents), "relation_count": len(rels), "report_count": len(reports)})
}

type kgEmbedDriver struct {
	models.ModelDriver
	calls   int
	invalid bool
	cancel  context.CancelFunc
}

func (d *kgEmbedDriver) Embed(_ *string, _ []string, api *models.APIConfig, cfg *models.EmbeddingConfig) ([]models.EmbeddingData, error) {
	d.calls++
	if api.Context == nil || !cfg.Query {
		return nil, errors.New("missing query context")
	}
	if d.cancel != nil {
		d.cancel()
	}
	v := 1.0
	if d.invalid {
		v = math.NaN()
	}
	return []models.EmbeddingData{{Index: 0, Embedding: []float64{v, 0}}}, nil
}
func TestKGEmbeddingContract(t *testing.T) {
	name := "embed"
	driver := &kgEmbedDriver{}
	model := models.NewEmbeddingModel(driver, &name, &models.APIConfig{})
	model.EmbeddingConfig = &models.EmbeddingConfig{Dimension: 2}
	dense, err := buildKGDenseExpr(t.Context(), model, "q", 2)
	if err != nil || dense.VectorColumnName != "q_2_vec" || dense.EmbeddingData[1] != 0 {
		t.Fatal(dense, err)
	}
	if model.APIConfig.Context != nil || model.EmbeddingConfig.Query {
		t.Fatal("shared binding mutated")
	}
	driver.invalid = true
	if _, err := buildKGDenseExpr(t.Context(), model, "q", 2); err == nil {
		t.Fatal("invalid vector accepted")
	}
	driver.invalid = false
	ctx, cancel := context.WithCancel(t.Context())
	driver.cancel = cancel
	if _, err := buildKGDenseExpr(ctx, model, "q", 2); !errors.Is(err, context.Canceled) {
		t.Fatal("cancellation lost", err)
	}
	store := &kgStore{}
	svc := &KGSearchService{store: store, kbReader: &kgReader{deny: "kb-b"}}
	before := driver.calls
	if _, err := svc.SearchKGEntities(t.Context(), "owner", []string{"kb-b"}, "q", model, 2); err == nil || driver.calls != before || store.calls != 0 {
		t.Fatal("unauthorized embedding side effect", err)
	}
}

func TestKGParsersRejectMalformedNumbersAndKeepArrays(t *testing.T) {
	rows := ParseKGEntityChunks([]map[string]any{{"entity_kwd": []string{"A"}, "rank_flt": 0.0}, {"entity_kwd": "bad", "rank_flt": "bad"}, {"entity_kwd": "nan", "_score": math.NaN()}})
	if len(rows) != 1 || rows[0].Name != "A" || rows[0].PageRank != 0 {
		t.Fatal(rows)
	}
	rels := ParseKGRelationChunks([]map[string]any{{"from_entity_kwd": []string{"A"}, "to_entity_kwd": []string{"B"}, "weight_int": 0}, {"from_entity_kwd": "A", "to_entity_kwd": "B", "weight_int": "bad"}, {"from_entity_kwd": "A", "to_entity_kwd": "B", "weight_int": 1.5}})
	if len(rels) != 1 || rels[0].Weight != 0 {
		t.Fatal(rels)
	}
	reports := ParseKGCommunityReportChunks([]map[string]any{{"docnm_kwd": "report", "entities_kwd": []string{"A", "B"}}})
	if len(reports) != 1 || !reflect.DeepEqual(reports[0].EntityNames, []string{"A", "B"}) {
		t.Fatal(reports)
	}
	samples := ParseKGTypeSamplesChunks([]map[string]any{{"content_with_weight": `{"person":[null,"A"],"": ["bad"],"null":null}`}})
	if !reflect.DeepEqual(samples, map[string][]string{"person": {"A"}}) {
		t.Fatal(samples)
	}
}

type kgPerDatasetStore struct {
	engine.DocEngine
	calls int
}

func (s *kgPerDatasetStore) Search(_ context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	s.calls++
	if len(req.KbIDs) != 1 {
		return nil, errors.New("Infinity merges after truncation")
	}
	name := "low"
	weight := 1.0
	if req.KbIDs[0] == "kb-high" {
		name = "high"
		weight = 100
	}
	return &types.SearchResult{Chunks: []map[string]any{{"entity_kwd": name, "rank_flt": weight, "docnm_kwd": name, "weight_flt": weight}}}, nil
}
func TestKGGlobalLimitAcrossSameTenantDatasets(t *testing.T) {
	store := &kgPerDatasetStore{}
	svc := &KGSearchService{store: store, kbReader: &kgReader{}}
	ents, err := svc.SearchKGEntitiesByTypes(t.Context(), "owner", []string{"kb-low", "kb-high"}, []string{"person"}, 1)
	if err != nil || len(ents) != 1 || ents[0].Name != "high" {
		t.Fatal(ents, err)
	}
	reports, err := svc.SearchKGCommunityReports(t.Context(), "owner", []string{"kb-low", "kb-high"}, []string{"A"}, 1)
	if err != nil || len(reports) != 1 || reports[0].Title != "high" || store.calls != 4 {
		t.Fatal(reports, err, store.calls)
	}
}
