package service

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/engine/elasticsearch"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/testsupport"
)

type after98MetadataLiveEngine struct {
	engine.DocEngine
	calls       int
	afterSearch func()
}

func (e *after98MetadataLiveEngine) Search(ctx context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	e.calls++
	result, err := e.DocEngine.Search(ctx, req)
	if e.afterSearch != nil {
		e.afterSearch()
	}
	return result, err
}

func TestAfter98MetadataSQLAndElasticsearch(t *testing.T) {
	path := os.Getenv("MULTIRAG_AFTER98_G_CONFIG")
	if path == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned G SQL/ES config required")
		}
		t.Skip("owned G SQL/ES config required")
	}
	journal := testsupport.NewCase(t)
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("private G config unavailable")
	}
	var cfg struct {
		Host, User, Password, Database string
		Port                           int
		Elasticsearch                  server.ElasticsearchConfig
	}
	if json.Unmarshal(raw, &cfg) != nil || cfg.Host != "127.0.0.1" || cfg.Port != 5432 || cfg.Database != "multirag_batch_g_after98_20261029" || cfg.Elasticsearch.Hosts != "http://localhost:1200" {
		t.Fatal("exact owned TCP database and borrowed ES endpoint required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	endpoint := url.URL{Scheme: "postgres", User: url.UserPassword(cfg.User, cfg.Password), Host: "127.0.0.1:5432", Path: cfg.Database, RawQuery: "sslmode=disable"}
	tables := []string{"t_ai_knowledgebases", "t_ai_user_tenants"}
	journal.Event("reserve-schema-tables-before-AutoMigrate", map[string]any{"host": cfg.Host, "port": cfg.Port, "database": cfg.Database, "schema": "public", "tables": tables})
	db, err := gorm.Open(postgres.Open(endpoint.String()), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("owned SQL connection failed")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = pool.Close() })
	oldDB := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = oldDB })
	read, err := sql.Open("pgx", endpoint.String())
	if err != nil {
		t.Fatal("independent SQL connection failed")
	}
	t.Cleanup(func() { _ = read.Close() })
	if err := db.WithContext(ctx).AutoMigrate(&entity.Knowledgebase{}, &entity.UserTenant{}); err != nil {
		t.Fatal("owned schema setup failed")
	}
	for _, table := range tables {
		var count int
		if err := read.QueryRowContext(ctx, "SELECT count(*) FROM "+table).Scan(&count); err != nil || count != 0 {
			t.Fatal("owned fixture table was not empty")
		}
	}
	reader, active := "after98-g-reader", "1"
	tenants := []string{"after98_g_20261029_a", "after98_g_20261029_b", "after98_g_20261029_c"}
	kbs := []string{"000000000000100080000000000000a1", "000000000000100080000000000000b1", "000000000000100080000000000000c1"}
	members := []string{"after98-g-member-a", "after98-g-member-b"}
	journal.Event("reserve-SQL-identities-before-INSERT", map[string]any{"tenant_ids": tenants, "dataset_ids": kbs, "user_ids": []string{reader, "after98-g-owner"}, "membership_ids": members})
	t.Cleanup(func() {
		cleanup, stop := context.WithTimeout(context.Background(), 15*time.Second)
		defer stop()
		if _, err := read.ExecContext(cleanup, "DELETE FROM t_ai_user_tenants WHERE id IN ($1,$2)", members[0], members[1]); err != nil {
			t.Error("owned membership cleanup failed")
		}
		if _, err := read.ExecContext(cleanup, "DELETE FROM t_ai_knowledgebases WHERE id IN ($1,$2,$3)", kbs[0], kbs[1], kbs[2]); err != nil {
			t.Error("owned dataset cleanup failed")
		}
		counts := map[string]int{}
		for _, table := range tables {
			var count int
			if err := read.QueryRowContext(cleanup, "SELECT count(*) FROM "+table).Scan(&count); err != nil {
				t.Error("independent SQL cleanup readback failed")
			}
			counts[table] = count
			if count != 0 {
				t.Error("owned SQL fixture rows survived cleanup")
			}
		}
		journal.Event("SQL-cleanup-independent-readback", map[string]any{"database": cfg.Database, "counts": counts})
	})
	for i, kb := range kbs {
		if err := db.WithContext(ctx).Create(&entity.Knowledgebase{ID: kb, TenantID: tenants[i], CreatedBy: "after98-g-owner", Permission: "team", Status: &active}).Error; err != nil {
			t.Fatal("dataset INSERT failed")
		}
		if i < 2 {
			if err := db.WithContext(ctx).Create(&entity.UserTenant{ID: members[i], UserID: reader, TenantID: tenants[i], Role: "normal", Status: &active}).Error; err != nil {
				t.Fatal("membership INSERT failed")
			}
		}
	}
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	client := &http.Client{Timeout: 10 * time.Second}
	t.Cleanup(client.CloseIdleConnections)
	request := func(callctx context.Context, method, suffix string) *http.Response {
		t.Helper()
		r, err := http.NewRequestWithContext(callctx, method, cfg.Elasticsearch.Hosts+suffix, nil)
		if err != nil {
			t.Fatal(err)
		}
		r.SetBasicAuth(cfg.Elasticsearch.Username, cfg.Elasticsearch.Password)
		response, err := client.Do(r)
		if err != nil {
			t.Fatal("independent ES request failed")
		}
		return response
	}
	store, err := elasticsearch.NewEngine(&cfg.Elasticsearch)
	if err != nil {
		t.Fatal("borrowed ES unavailable")
	}
	t.Cleanup(func() {
		if err := store.Close(); err != nil {
			t.Error("ES client close failed")
		}
	})
	owned := []string{}
	t.Cleanup(func() {
		cleanup, stop := context.WithTimeout(context.Background(), 20*time.Second)
		defer stop()
		for _, tenant := range owned {
			index := BuildMetadataIndexName(tenant)
			before := request(cleanup, http.MethodHead, "/"+index)
			before.Body.Close()
			if before.StatusCode == http.StatusOK {
				journal.Event("reserve-owned-ES-cleanup-before-drop", map[string]any{"index": index})
				if err := store.DropMetadataStore(cleanup, tenant); err != nil {
					t.Error("owned ES metadata cleanup failed")
				}
			} else if before.StatusCode != http.StatusNotFound {
				t.Error("owned ES pre-cleanup readback failed")
			}
			response := request(cleanup, http.MethodHead, "/"+index)
			response.Body.Close()
			if response.StatusCode != http.StatusNotFound {
				t.Error("owned ES index survived cleanup")
			}
			journal.Event("ES-cleanup-independent-HEAD", map[string]any{"index": index, "status": response.StatusCode})
		}
	})
	for i, tenant := range tenants {
		index := BuildMetadataIndexName(tenant)
		row := map[string]any{"id": "shared-doc", "kb_id": kbs[i], "meta_fields": map[string]any{"author": "author-" + string(rune('a'+i)), "boolean": false, "number": 0, "empty": "", "labels": []string{"x", "y"}, "large": json.Number("9007199254740993")}}
		input, _ := json.Marshal(row)
		journal.Event("reserve-ES-index-and-logical-row-before-create", map[string]any{"endpoint": cfg.Elasticsearch.Hosts, "index": index, "kb_id": kbs[i], "document_id": "shared-doc", "inputSHA256": fmt.Sprintf("%x", sha256.Sum256(input))})
		response := request(ctx, http.MethodHead, "/"+index)
		response.Body.Close()
		if response.StatusCode != http.StatusNotFound {
			t.Fatal("owned ES index already exists")
		}
		owned = append(owned, tenant)
		if err := store.CreateMetadataStore(ctx, tenant); err != nil {
			t.Fatal(err)
		}
		if failed, err := store.InsertMetadata(ctx, []map[string]any{row}, tenant); err != nil || len(failed) != 0 {
			t.Fatal("owned metadata INSERT failed")
		}
	}
	wrapped := &after98MetadataLiveEngine{DocEngine: store}
	svc := &MetadataService{kbDAO: dao.NewKnowledgebaseDAO(), docEngine: wrapped}
	chunks := []map[string]any{{"id": "chunk-a", "doc_id": "shared-doc", "kb_id": kbs[0], "document_metadata": map[string]any{"stale": true}}, {"id": "chunk-a2", "doc_id": "shared-doc", "kb_id": kbs[0]}, {"id": "chunk-b", "doc_id": "shared-doc", "kb_id": kbs[1]}, {"id": "missing", "doc_id": "missing-doc", "kb_id": kbs[0]}}
	snapshot := referenceMetadataSnapshot(t, chunks)
	got, err := svc.EnrichChunksWithDocMetadata(ctx, chunks, reader, nil)
	if err != nil || len(got) != 4 || wrapped.calls != 2 {
		t.Fatal("real SQL/ES enrichment failed", err)
	}
	for i, author := range []string{"author-a", "author-a", "author-b"} {
		if got[i]["document_metadata"].(map[string]any)["author"] != author {
			t.Fatal("tenant/document identity crossed")
		}
	}
	if got[3]["document_metadata"] != nil {
		t.Fatal("missing document gained metadata")
	}
	got[0]["document_metadata"].(map[string]any)["labels"].([]any)[0] = "changed"
	if got[1]["document_metadata"].(map[string]any)["labels"].([]any)[0] != "x" || snapshot != referenceMetadataSnapshot(t, chunks) {
		t.Fatal("enrichment copies aliased original or sibling")
	}
	for _, fields := range [][]string{{"author"}, {"absent"}, {}} {
		before := wrapped.calls
		got, err := svc.EnrichChunksWithDocMetadata(ctx, chunks, reader, fields)
		if err != nil || snapshot != referenceMetadataSnapshot(t, chunks) {
			t.Fatal("field selection changed input", err)
		}
		if len(fields) == 0 || fields[0] == "absent" {
			if got[0]["document_metadata"] != nil || len(fields) == 0 && wrapped.calls != before {
				t.Fatal("empty field selection widened or performed IO")
			}
		} else if !reflect.DeepEqual(got[0]["document_metadata"], map[string]any{"author": "author-a"}) {
			t.Fatal("selected fields widened")
		}
	}
	denied := []map[string]any{chunks[0], {"id": "denied", "doc_id": "shared-doc", "kb_id": kbs[2]}}
	before := wrapped.calls
	if got, err := svc.EnrichChunksWithDocMetadata(ctx, denied, reader, nil); err == nil || got != nil || wrapped.calls != before {
		t.Fatal("denied scope reached ES")
	}
	canceled, stop := context.WithCancel(ctx)
	stop()
	if got, err := svc.EnrichChunksWithDocMetadata(canceled, chunks, reader, nil); !errors.Is(err, context.Canceled) || got != nil || wrapped.calls != before {
		t.Fatal("early cancellation performed IO")
	}
	canceled, stop = context.WithCancel(ctx)
	wrapped.afterSearch = stop
	if got, err := svc.EnrichChunksWithDocMetadata(canceled, chunks, reader, nil); !errors.Is(err, context.Canceled) || got != nil {
		t.Fatal("in-flight cancellation yielded partial success")
	}
	stop()
	wrapped.afterSearch = nil
	directory, err := svc.GetFlattedMetaByKBsContext(ctx, kbs[:2])
	if err != nil || directory["author"]["author-b"] == nil || directory["boolean"]["false"] == nil || directory["number"]["0"] == nil || directory["empty"][""] == nil || directory["labels"]["x"] == nil || directory["large"]["9007199254740993"] == nil {
		t.Fatal("real typed metadata producer lost values", err)
	}
	for _, operand := range []json.Number{"9007199254740993", "9007199254740992"} {
		ids, empty, err := ApplyMetaDataFilterChecked(ctx, manualMetadataConfig([]MetaFilterCondition{{Key: "large", Op: "=", Value: operand}}, "and"), directory, "q", nil, nil)
		if err != nil || operand == "9007199254740993" && (empty || !reflect.DeepEqual(ids, []string{"shared-doc"})) || operand == "9007199254740992" && (!empty || !reflect.DeepEqual(ids, []string{"-999"})) {
			t.Fatal("exact large numeric filter lost distinction")
		}
	}
	flat, code, err := (&DatasetsService{kbDAO: dao.NewKnowledgebaseDAO()}).FlattenMetadata(ctx, kbs[:2], reader, wrapped)
	if err != nil || code != common.CodeSuccess {
		t.Fatal("active dataset metadata consumer rejected exact numbers", err)
	}
	for field, value := range map[string]string{"large": "9007199254740993", "number": "0", "boolean": "false"} {
		labels, ok := flat[field].(map[string]any)
		if !ok || !reflect.DeepEqual(labels[value], []string{"shared-doc"}) {
			t.Fatal("active dataset metadata consumer lost scalar", field, flat[field])
		}
	}
	journal.Event("dataset-metadata-consumer-independent-readback", map[string]any{"large": "9007199254740993", "zero": "0", "false": "false", "business_code": code})
	consumer := NewChunkService(wrapped)
	req := &RetrievalTestRequest{DatasetIDs: kbs[:2], Question: "q", Filter: manualMetadataConfig([]MetaFilterCondition{{Key: "author", Op: "=", Value: "absent"}}, "and")}
	if result, err := consumer.RetrievalTest(ctx, req, reader); err != nil || result == nil || result.Total != 0 {
		t.Fatal("active no-match retrieval escaped early boundary", err)
	}
	before = wrapped.calls
	req.DocIDs = []string{}
	if result, err := consumer.RetrievalTest(ctx, req, reader); err != nil || result == nil || result.Total != 0 || wrapped.calls != before {
		t.Fatal("explicit empty scope performed retrieval IO")
	}
	req.DatasetIDs = []string{kbs[0], kbs[2]}
	if _, err := consumer.RetrievalTest(ctx, req, reader); !errors.Is(err, ErrDatasetAccessDenied) || wrapped.calls != before {
		t.Fatal("denied active scope reached ES")
	}
	for i, tenant := range tenants {
		response := request(ctx, http.MethodGet, "/"+BuildMetadataIndexName(tenant)+"/_search?size=10")
		var found struct {
			Hits struct {
				Hits []struct {
					Source map[string]any `json:"_source"`
				} `json:"hits"`
			} `json:"hits"`
		}
		decoder := json.NewDecoder(response.Body)
		decoder.UseNumber()
		err := decoder.Decode(&found)
		response.Body.Close()
		if response.StatusCode != 200 || err != nil || len(found.Hits.Hits) != 1 {
			t.Fatal("independent ES readback failed")
		}
		row := found.Hits.Hits[0].Source
		fields := row["meta_fields"].(map[string]any)
		if row["id"] != "shared-doc" || row["kb_id"] != kbs[i] || fields["large"] != json.Number("9007199254740993") || fields["boolean"] != false || fields["number"] != json.Number("0") || fields["empty"] != "" {
			t.Fatal("independent ES values changed")
		}
		journal.Event("ES-independent-document-readback", map[string]any{"index": BuildMetadataIndexName(tenant), "kb_id": kbs[i], "id": row["id"], "fields": fields})
	}
	for i, kb := range kbs {
		var tenant, owner, permission, status string
		if err := read.QueryRowContext(ctx, "SELECT tenant_id,created_by,permission,status FROM t_ai_knowledgebases WHERE id=$1", kb).Scan(&tenant, &owner, &permission, &status); err != nil || tenant != tenants[i] || owner != "after98-g-owner" || permission != "team" || status != active {
			t.Fatal("independent dataset readback changed")
		}
	}
	for i, id := range members {
		var user, tenant, role, status string
		if err := read.QueryRowContext(ctx, "SELECT user_id,tenant_id,role,status FROM t_ai_user_tenants WHERE id=$1", id).Scan(&user, &tenant, &role, &status); err != nil || user != reader || tenant != tenants[i] || role != "normal" || status != active {
			t.Fatal("independent membership readback changed")
		}
	}
	for table, want := range map[string]int{tables[0]: 3, tables[1]: 2} {
		var count int
		if err := read.QueryRowContext(ctx, "SELECT count(*) FROM "+table).Scan(&count); err != nil || count != want {
			t.Fatal("forbidden SQL side effect")
		}
	}
	journal.Event("SQL-independent-readback", map[string]any{"dataset_count": 3, "membership_count": 2, "unexpected_writes": 0, "denied_ES_calls": 0, "source_unchanged": snapshot == referenceMetadataSnapshot(t, chunks)})
	// A real missing index after the first tenant succeeds must yield no partial result.
	journal.Event("reserve-owned-index-drop-before-failure-case", map[string]any{"index": BuildMetadataIndexName(tenants[1])})
	if err := store.DropMetadataStore(ctx, tenants[1]); err != nil {
		t.Fatal(err)
	}
	response := request(ctx, http.MethodHead, "/"+BuildMetadataIndexName(tenants[1]))
	response.Body.Close()
	if response.StatusCode != http.StatusNotFound {
		t.Fatal("failure fixture index still exists")
	}
	journal.Event("owned-index-dropped-before-failure-case", map[string]any{"index": BuildMetadataIndexName(tenants[1]), "status": response.StatusCode})
	if got, err := svc.EnrichChunksWithDocMetadata(ctx, chunks, reader, nil); err == nil || got != nil || !strings.Contains(err.Error(), "search") || snapshot != referenceMetadataSnapshot(t, chunks) {
		t.Fatal("real storage failure yielded partial result or changed input")
	}
}
