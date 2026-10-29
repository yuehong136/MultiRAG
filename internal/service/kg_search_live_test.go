package service

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"reflect"
	"sort"
	"sync/atomic"
	"testing"
	"time"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/engine/elasticsearch"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/testsupport"
)

type kgLiveCountingEngine struct {
	engine.DocEngine
	t       *testing.T
	journal *testsupport.CaseJournal
	index   string
	calls   atomic.Int64
}

func (s *kgLiveCountingEngine) Search(ctx context.Context, request *types.SearchRequest) (*types.SearchResult, error) {
	s.calls.Add(1)
	if !reflect.DeepEqual(request.IndexNames, []string{s.index}) || len(request.KbIDs) == 0 {
		s.t.Error("live KG request lost its exact index/dataset scope")
	}
	if request.Filter["knowledge_graph_kwd"] == nil || len(request.SelectFields) == 0 {
		s.t.Error("live KG request lost its graph kind or projection")
	}
	raw, err := json.Marshal(request)
	if err != nil {
		return nil, err
	}
	s.journal.Event("KG-live-engine-request", map[string]any{"sha256": kgLiveSHA(raw), "request": request})
	result, err := s.DocEngine.Search(ctx, request)
	if err == nil && result != nil {
		for _, row := range result.Chunks {
			if _, exists := row["unprojected_marker"]; exists {
				s.t.Error("real ES returned an unselected source field")
			}
		}
	}
	return result, err
}

func kgLiveSHA(raw []byte) string {
	value := sha256.Sum256(raw)
	return hex.EncodeToString(value[:])
}

// TestKGLiveSQLAndES uses only the precreated, registered TCP scratch database
// and an absent exact ES index. The caller owns dropping the scratch database.
func TestKGLiveSQLAndES(t *testing.T) {
	configPath := os.Getenv("MULTIRAG_KG_LIVE_CONFIG")
	if configPath == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("private owned KG live config required")
		}
		t.Skip("private owned KG live config required")
	}
	journal := testsupport.NewCase(t)
	raw, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal("private KG live config unavailable")
	}
	var cfg struct {
		Tenant   string
		Database struct {
			Host, User, Password, Name string
			Port                       int
		}
		ES server.ElasticsearchConfig
	}
	if json.Unmarshal(raw, &cfg) != nil || cfg.Tenant != "after98_k_20261029" || cfg.Database.Host != "127.0.0.1" || cfg.Database.Port != 5432 || cfg.Database.Name != "multirag_after98_k_20261029" || cfg.ES.Hosts != "http://127.0.0.1:1200" {
		t.Fatal("exact owned KG SQL/ES endpoints required")
	}
	index := "multirag_" + cfg.Tenant
	endpoint := url.URL{Scheme: "postgres", User: url.UserPassword(cfg.Database.User, cfg.Database.Password), Host: "127.0.0.1:5432", Path: cfg.Database.Name, RawQuery: "sslmode=disable&connect_timeout=5"}
	journal.Event("reserve-KG-live-schema-before-SQL", map[string]any{"host": cfg.Database.Host, "port": cfg.Database.Port, "database": cfg.Database.Name, "schema": "public", "tables": []string{"t_ai_knowledgebases", "t_ai_user_tenants"}, "database_drop_owner": "K parent"})
	db, err := gorm.Open(postgres.Open(endpoint.String()), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("KG scratch SQL connection failed")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal("KG scratch SQL pool unavailable")
	}
	t.Cleanup(func() { _ = pool.Close() })
	ctx, cancel := context.WithTimeout(t.Context(), 90*time.Second)
	defer cancel()
	db = db.WithContext(ctx)
	read, err := sql.Open("pgx", endpoint.String())
	if err != nil {
		t.Fatal("independent KG SQL connection failed")
	}
	t.Cleanup(func() { _ = read.Close() })
	var existingTables int
	if err := read.QueryRow("SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'").Scan(&existingTables); err != nil || existingTables != 0 {
		t.Fatal("KG scratch database must initially have no public tables")
	}
	if err := db.AutoMigrate(&entity.Knowledgebase{}, &entity.UserTenant{}); err != nil {
		t.Fatal("KG scratch schema migration failed")
	}
	previousDB := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = previousDB })
	const owner, member, invite, inactive, foreign = "kg-live-owner", "kg-live-member", "kg-live-invite", "kg-live-inactive", "kg-live-foreign"
	const privateKB, teamKB, excludedKB, deletedKB, foreignPrivateKB = "kg-live-private", "kg-live-team", "kg-live-excluded", "kg-live-deleted", "kg-live-foreign-private"
	active, disabled := "1", "0"
	journal.Event("reserve-KG-live-fixtures-before-INSERT", map[string]any{"tenant_ids": []string{cfg.Tenant, "kg-live-foreign-tenant"}, "user_ids": []string{owner, member, invite, inactive, foreign}, "dataset_ids": []string{privateKB, teamKB, excludedKB, deletedKB, foreignPrivateKB}, "membership_ids": []string{owner, member, invite, inactive, foreign}})
	create := func(value any) {
		t.Helper()
		if err := db.Create(value).Error; err != nil {
			t.Fatalf("KG fixture INSERT failed: %T", value)
		}
	}
	for _, row := range []struct{ user, tenant, role, status string }{{owner, cfg.Tenant, "owner", active}, {member, cfg.Tenant, "normal", active}, {invite, cfg.Tenant, "invite", active}, {inactive, cfg.Tenant, "normal", disabled}, {foreign, "kg-live-foreign-tenant", "owner", active}} {
		status := row.status
		create(&entity.UserTenant{ID: row.user, UserID: row.user, TenantID: row.tenant, Role: row.role, InvitedBy: owner, Status: &status})
	}
	for _, row := range []struct{ id, creator, permission, status string }{{privateKB, owner, "me", active}, {teamKB, owner, "team", active}, {excludedKB, owner, "team", active}, {deletedKB, owner, "team", disabled}, {foreignPrivateKB, foreign, "me", active}} {
		status := row.status
		create(&entity.Knowledgebase{ID: row.id, TenantID: cfg.Tenant, Name: row.id, CreatedBy: row.creator, Permission: row.permission, EmbdID: "not-used", Status: &status})
	}
	var privateCreator, permission string
	if err := read.QueryRow("SELECT created_by, permission FROM t_ai_knowledgebases WHERE id=$1 AND tenant_id=$2", privateKB, cfg.Tenant).Scan(&privateCreator, &permission); err != nil || privateCreator != owner || permission != "me" {
		t.Fatal("independent private dataset SQL readback failed")
	}
	var activeMembers int
	if err := read.QueryRow("SELECT count(*) FROM t_ai_user_tenants WHERE tenant_id=$1 AND status='1' AND role IN ('owner','admin','normal')", cfg.Tenant).Scan(&activeMembers); err != nil || activeMembers != 2 {
		t.Fatal("independent active membership SQL readback failed")
	}
	journal.Event("KG-live-SQL-fixture-readback", map[string]any{"private_dataset": privateKB, "private_owner": privateCreator, "private_permission": permission, "active_member_count": activeMembers})

	writeTransport, readTransport := &http.Transport{}, &http.Transport{}
	writeHTTP := &http.Client{Transport: writeTransport, Timeout: 5 * time.Second}
	readHTTP := &http.Client{Transport: readTransport, Timeout: 5 * time.Second}
	t.Cleanup(writeTransport.CloseIdleConnections)
	t.Cleanup(readTransport.CloseIdleConnections)
	request := func(client *http.Client, method, path string, payload []byte) (int, []byte, error) {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		req, err := http.NewRequestWithContext(ctx, method, cfg.ES.Hosts+path, bytes.NewReader(payload))
		if err != nil {
			return 0, nil, fmt.Errorf("KG ES request could not be constructed")
		}
		if cfg.ES.Username != "" || cfg.ES.Password != "" {
			req.SetBasicAuth(cfg.ES.Username, cfg.ES.Password)
		}
		if payload != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		response, err := client.Do(req)
		if err != nil {
			return 0, nil, fmt.Errorf("KG ES request transport failed")
		}
		defer response.Body.Close()
		body, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
		return response.StatusCode, body, err
	}
	status, _, err := request(readHTTP, http.MethodHead, "/"+index, nil)
	if err != nil || status != http.StatusNotFound {
		t.Fatal("owned KG ES index must be absent before reservation")
	}
	mapping := map[string]any{"settings": map[string]any{"number_of_shards": 1, "number_of_replicas": 0}, "mappings": map[string]any{"properties": map[string]any{
		"kb_id": map[string]any{"type": "keyword"}, "available_int": map[string]any{"type": "integer"}, "knowledge_graph_kwd": map[string]any{"type": "keyword"},
		"entity_kwd": map[string]any{"type": "keyword"}, "entity_type_kwd": map[string]any{"type": "keyword"}, "from_entity_kwd": map[string]any{"type": "keyword"}, "to_entity_kwd": map[string]any{"type": "keyword"},
		"rank_flt": map[string]any{"type": "double"}, "weight_int": map[string]any{"type": "integer"}, "weight_flt": map[string]any{"type": "double"},
		"content_ltks": map[string]any{"type": "text"}, "content_with_weight": map[string]any{"type": "text", "index": false}, "n_hop_with_weight": map[string]any{"type": "text", "index": false},
		"docnm_kwd": map[string]any{"type": "keyword"}, "entities_kwd": map[string]any{"type": "keyword"}, "unprojected_marker": map[string]any{"type": "keyword"},
	}}}
	mappingJSON, err := json.Marshal(mapping)
	if err != nil {
		t.Fatal(err)
	}
	documents := map[string]map[string]any{
		"entity-zero":          {"kb_id": privateKB, "knowledge_graph_kwd": "entity", "entity_kwd": "Alpha", "entity_type_kwd": "person", "rank_flt": 0, "content_ltks": "alpha", "content_with_weight": "zero entity", "n_hop_with_weight": "[]"},
		"entity-team":          {"kb_id": teamKB, "knowledge_graph_kwd": "entity", "entity_kwd": "Beta", "entity_type_kwd": "person", "rank_flt": 3, "content_ltks": "alpha beta", "content_with_weight": "team entity", "n_hop_with_weight": "[]"},
		"entity-other-type":    {"kb_id": privateKB, "knowledge_graph_kwd": "entity", "entity_kwd": "OtherType", "entity_type_kwd": "place", "rank_flt": 999, "content_ltks": "zeta", "content_with_weight": "outside requested entity type"},
		"entity-excluded":      {"kb_id": excludedKB, "knowledge_graph_kwd": "entity", "entity_kwd": "Intruder", "entity_type_kwd": "person", "rank_flt": 999, "content_ltks": "alpha", "content_with_weight": "outside requested KB"},
		"plain-distractor":     {"kb_id": privateKB, "knowledge_graph_kwd": "document", "entity_kwd": "PlainIntruder", "entity_type_kwd": "person", "rank_flt": 999, "content_ltks": "alpha", "content_with_weight": "outside KG entity kind"},
		"relation-zero":        {"kb_id": privateKB, "knowledge_graph_kwd": "relation", "from_entity_kwd": []string{"Alpha"}, "to_entity_kwd": []string{"Beta"}, "weight_int": 0, "content_ltks": "alpha beta", "content_with_weight": "zero relation"},
		"relation-excluded":    {"kb_id": excludedKB, "knowledge_graph_kwd": "relation", "from_entity_kwd": "Intruder", "to_entity_kwd": "Intruder2", "weight_int": 999, "content_ltks": "alpha", "content_with_weight": "outside requested relation KB"},
		"community-low":        {"kb_id": privateKB, "knowledge_graph_kwd": "community_report", "docnm_kwd": "low report", "content_with_weight": "low community", "weight_flt": 0, "entities_kwd": []string{"Alpha", "Beta"}},
		"community-high":       {"kb_id": teamKB, "knowledge_graph_kwd": "community_report", "docnm_kwd": "high report", "content_with_weight": "high community", "weight_flt": 10, "entities_kwd": []string{"Alpha", "Beta"}},
		"community-other-name": {"kb_id": privateKB, "knowledge_graph_kwd": "community_report", "docnm_kwd": "excluded name report", "content_with_weight": "outside requested entity name", "weight_flt": 999, "entities_kwd": []string{"Other"}},
		"type-private":         {"kb_id": privateKB, "knowledge_graph_kwd": "ty2ents", "content_with_weight": `{"person":["Alpha"]}`},
		"type-team":            {"kb_id": teamKB, "knowledge_graph_kwd": "ty2ents", "content_with_weight": `{"person":["Beta"]}`},
		"type-excluded":        {"kb_id": excludedKB, "knowledge_graph_kwd": "ty2ents", "content_with_weight": `{"rogue":["Intruder"]}`},
	}
	documentJSON := make(map[string][]byte, len(documents))
	documentHashes := make(map[string]string, len(documents))
	documentIDs := make([]string, 0, len(documents))
	for id, document := range documents {
		document["available_int"], document["unprojected_marker"] = 1, "must-not-be-returned"
		encoded, err := json.Marshal(document)
		if err != nil {
			t.Fatal(err)
		}
		documentJSON[id], documentHashes[id] = encoded, kgLiveSHA(encoded)
		documentIDs = append(documentIDs, id)
	}
	sort.Strings(documentIDs)
	journal.Event("reserve-KG-live-index-before-create", map[string]any{"endpoint": cfg.ES.Hosts, "index": index, "preflight_HEAD_status": status, "mapping_sha256": kgLiveSHA(mappingJSON), "document_ids": documentIDs, "document_sha256": documentHashes})
	// Reservation follows an independent HEAD 404. Cleanup owns only this exact
	// name, including the uncertain outcome of a failed create request.
	t.Cleanup(func() {
		deleteStatus, _, deleteErr := request(writeHTTP, http.MethodDelete, "/"+index, nil)
		absentStatus, _, absentErr := request(readHTTP, http.MethodHead, "/"+index, nil)
		if deleteErr != nil || (deleteStatus != http.StatusOK && deleteStatus != http.StatusNotFound) || absentErr != nil || absentStatus != http.StatusNotFound {
			t.Error("exact KG ES index cleanup or independent absence readback failed")
		}
		journal.Event("KG-live-index-cleanup-readback", map[string]any{"index": index, "delete_status": deleteStatus, "independent_HEAD_status": absentStatus, "absent": absentErr == nil && absentStatus == http.StatusNotFound})
	})
	if status, _, err := request(writeHTTP, http.MethodPut, "/"+index, mappingJSON); err != nil || status != http.StatusOK {
		t.Fatal("owned KG ES index creation failed")
	}
	for _, id := range documentIDs {
		if status, _, err := request(writeHTTP, http.MethodPut, "/"+index+"/_doc/"+id+"?refresh=wait_for", documentJSON[id]); err != nil || (status != http.StatusCreated && status != http.StatusOK) {
			t.Fatalf("owned KG ES document creation failed: %s", id)
		}
	}
	store, err := elasticsearch.NewEngine(&cfg.ES)
	if err != nil {
		t.Fatal("owned KG ES engine initialization failed")
	}
	t.Cleanup(func() { _ = store.Close() })
	counting := &kgLiveCountingEngine{DocEngine: store, t: t, journal: journal, index: index}
	service := NewKGSearchService(counting)
	entities, err := service.SearchKGEntities(ctx, owner, []string{privateKB}, "alpha", nil, 10)
	if err != nil || len(entities) != 1 || entities[0].Name != "Alpha" || entities[0].Type != "person" || entities[0].PageRank != 0 || entities[0].NhopJSON != "[]" || entities[0].Description != "zero entity" {
		t.Fatal("real entity scope/projection/zero contract failed")
	}
	byType, err := service.SearchKGEntitiesByTypes(ctx, owner, []string{privateKB, teamKB}, []string{"person"}, 10)
	if err != nil || len(byType) != 2 || byType[0].Name != "Beta" || byType[1].Name != "Alpha" {
		t.Fatal("real entity type filter or cross-KB rank contract failed")
	}
	relations, err := service.SearchKGRelations(ctx, owner, []string{privateKB}, "alpha", nil, 10)
	if err != nil || len(relations) != 1 || relations[0].From != "Alpha" || relations[0].To != "Beta" || relations[0].Weight != 0 || relations[0].Description != "zero relation" {
		t.Fatal("real relation scope/projection/zero contract failed")
	}
	reports, err := service.SearchKGCommunityReports(ctx, owner, []string{privateKB, teamKB}, []string{"Alpha"}, 1)
	if err != nil || len(reports) != 1 || reports[0].Title != "high report" || reports[0].Weight != 10 || !reflect.DeepEqual(reports[0].EntityNames, []string{"Alpha", "Beta"}) {
		t.Fatal("real community global weight sort or array projection failed")
	}
	lowReports, err := service.SearchKGCommunityReports(ctx, owner, []string{privateKB}, []string{"Alpha"}, 1)
	if err != nil || len(lowReports) != 1 || lowReports[0].Weight != 0 {
		t.Fatal("real community legitimate zero weight lost")
	}
	samples, err := service.SearchKGTypeSamples(ctx, owner, []string{privateKB, teamKB})
	if err != nil || !reflect.DeepEqual(samples["person"], []string{"Alpha", "Beta"}) || len(samples) != 1 {
		t.Fatal("real type sample KB scope failed")
	}
	memberRows, err := service.SearchKGEntities(ctx, member, []string{teamKB}, "alpha", nil, 10)
	if err != nil || len(memberRows) != 1 || memberRows[0].Name != "Beta" {
		t.Fatal("active team member could not query the team dataset")
	}
	for _, denied := range []struct {
		actor string
		ids   []string
	}{{member, []string{privateKB}}, {invite, []string{teamKB}}, {inactive, []string{teamKB}}, {foreign, []string{teamKB}}, {owner, []string{deletedKB}}, {owner, []string{foreignPrivateKB}}, {owner, []string{"kg-live-missing"}}, {member, []string{teamKB, privateKB}}} {
		before := counting.calls.Load()
		if _, err := service.SearchKGEntities(ctx, denied.actor, denied.ids, "alpha", nil, 10); err == nil || counting.calls.Load() != before {
			t.Fatal("real SQL authorization denial reached the ES engine")
		}
		journal.Event("KG-live-denial-zero-engine-readback", map[string]any{"actor": denied.actor, "dataset_ids": denied.ids, "engine_call_delta": counting.calls.Load() - before})
	}
	before := counting.calls.Load()
	if rows, err := service.SearchKGEntities(ctx, owner, []string{}, "alpha", nil, 10); err != nil || len(rows) != 0 {
		t.Fatal("explicit empty dataset selection failed")
	}
	if rows, err := service.SearchKGEntities(ctx, owner, []string{privateKB}, "alpha", nil, 0); err != nil || len(rows) != 0 || counting.calls.Load() != before {
		t.Fatal("empty scope or zero limit reached the real engine")
	}
	// Independent GET uses a separate transport from both the writer and engine.
	for _, id := range documentIDs {
		status, body, err := request(readHTTP, http.MethodGet, "/"+index+"/_doc/"+id, nil)
		var stored struct {
			Source json.RawMessage `json:"_source"`
		}
		if err != nil || status != http.StatusOK || json.Unmarshal(body, &stored) != nil {
			t.Fatal("independent KG ES document readback failed")
		}
		var source map[string]any
		if json.Unmarshal(stored.Source, &source) != nil {
			t.Fatal("independent KG ES source decoding failed")
		}
		canonical, err := json.Marshal(source)
		if err != nil || kgLiveSHA(canonical) != documentHashes[id] {
			t.Fatal("KG queries changed an indexed document")
		}
	}
	status, countJSON, err := request(readHTTP, http.MethodGet, "/"+index+"/_count", nil)
	var count struct {
		Count int `json:"count"`
	}
	if err != nil || status != http.StatusOK || json.Unmarshal(countJSON, &count) != nil || count.Count != len(documentIDs) {
		t.Fatal("KG queries created or removed an ES document")
	}
	var datasetCount, membershipCount int
	if read.QueryRow("SELECT count(*) FROM t_ai_knowledgebases").Scan(&datasetCount) != nil || read.QueryRow("SELECT count(*) FROM t_ai_user_tenants").Scan(&membershipCount) != nil || datasetCount != 5 || membershipCount != 5 {
		t.Fatal("KG queries caused a forbidden SQL side effect")
	}
	journal.Event("KG-live-independent-readback", map[string]any{"index": index, "document_count": len(documentIDs), "document_sha256": documentHashes, "dataset_count": datasetCount, "membership_count": membershipCount, "engine_calls": counting.calls.Load(), "private_team_auth": true, "legitimate_zero": true, "KG_kind_and_KB_scope": true, "community_global_sort": true})
}
