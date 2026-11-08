package handler

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/google/uuid"
	"multirag/internal/common"
	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/service"
)

var errDatasetStoreBoundary = errors.New("controlled external store boundary")

type datasetIDStoreProbe struct {
	engine.DocEngine
	searches, reads int
}

func (s *datasetIDStoreProbe) Search(_ context.Context, _ *types.SearchRequest) (*types.SearchResult, error) {
	s.searches++
	return nil, errDatasetStoreBoundary
}
func (s *datasetIDStoreProbe) GetChunk(_ context.Context, _ string, _ string, _ []string) (any, error) {
	s.reads++
	return nil, errDatasetStoreBoundary
}

// Real production Go creation, real PostgreSQL readers/writes, and a rejecting
// external-store boundary. No fake success or provider credentials are used.
func TestRandomDatasetIDSQLLifecycleAndConsumerAuthorization(t *testing.T) {
	db, read, journal := modelIDScratchDB(t)
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := db.AutoMigrate(&entity.Tenant{}, &entity.User{}, &entity.UserTenant{}, &entity.Knowledgebase{}, &entity.Document{}, &entity.UserCanvas{}, &entity.Connector{}, &entity.Connector2Kb{}); err != nil {
		t.Fatal(err)
	}
	active := "1"
	for _, owner := range []string{"dataset-owner", "dataset-other"} {
		if err := db.Create(&entity.Tenant{ID: owner, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Create(&entity.User{ID: owner, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Create(&entity.UserTenant{ID: owner, UserID: owner, TenantID: owner, Role: "owner", Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	datasets := service.NewDatasetsService()
	created, code, err := datasets.CreateDataset(&service.CreateDatasetRequest{Name: "new random dataset"}, "dataset-owner")
	if err != nil || code != common.CodeSuccess {
		t.Fatal("actual CreateDataset", code, err)
	}
	id, ok := created["id"].(string)
	if !ok || len(id) != 32 {
		t.Fatal("missing generated ID", created)
	}
	parsed, err := uuid.Parse(id)
	if err != nil || parsed.Version() != 4 {
		t.Fatal("creation did not generate UUIDv4", id, err)
	}
	var storedID, storedName, storedOwner string
	if err := read.QueryRow("SELECT id,name,tenant_id FROM t_ai_knowledgebases WHERE id=$1", id).Scan(&storedID, &storedName, &storedOwner); err != nil || storedID != id || storedOwner != "dataset-owner" {
		t.Fatal("independent creation readback", err)
	}
	if row, code, err := datasets.GetDataset(t.Context(), id, "dataset-owner"); err != nil || code != 0 || row["id"] != id {
		t.Fatal("GET rejected generated v4", row, code, err)
	}
	rows, total, code, err := datasets.ListDatasets(id, "", 1, 30, "create_time", true, "", nil, "", "dataset-owner")
	if err != nil || code != 0 || total != 1 || len(rows) != 1 || rows[0]["id"] != id {
		t.Fatal("list rejected generated v4", rows, total, code, err)
	}
	kb := service.NewKnowledgebaseService()
	if _, code, err := kb.UpdateKB(&service.UpdateKBRequest{KBID: id, Name: "renamed random dataset", ParserID: "naive"}, "dataset-owner"); err != nil || code != 0 {
		t.Fatal("update rejected generated v4", code, err)
	}
	if err := read.QueryRow("SELECT name FROM t_ai_knowledgebases WHERE id=$1", id).Scan(&storedName); err != nil || storedName != "renamed random dataset" {
		t.Fatal("independent rename readback", err, storedName)
	}
	enabled := true
	if _, code, err := datasets.UpdateMetadataConfig(t.Context(), id, "dataset-owner", service.MetadataConfigRequest{Metadata: json.RawMessage(`[{"key":"label","type":"string"}]`), Enabled: &enabled}); err != nil || code != 0 {
		t.Fatal("metadata update rejected v4", code, err)
	}
	var label string
	var persistedEnabled bool
	if err := read.QueryRow("SELECT parser_config->'metadata'->0->>'key',(parser_config->>'enable_metadata')::boolean FROM t_ai_knowledgebases WHERE id=$1", id).Scan(&label, &persistedEnabled); err != nil || label != "label" || !persistedEnabled {
		t.Fatal("independent metadata readback", err, label, persistedEnabled)
	}
	if _, code, err := datasets.GetMetadataConfig(t.Context(), id, "dataset-owner"); err != nil || code != 0 {
		t.Fatal("metadata GET rejected v4", code, err)
	}
	docID := strings.ReplaceAll(uuid.NewString(), "-", "")
	docName := "document"
	if err := db.Create(&entity.Document{ID: docID, KbID: id, ParserID: "naive", ParserConfig: entity.JSONMap{}, CreatedBy: "dataset-owner", Type: "doc", Name: &docName, Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	documents := service.NewDocumentService(nil)
	if page, code, err := documents.ListDatasetDocuments(t.Context(), id, "dataset-owner", 1, 30, "", true); err != nil || code != 0 || page.Total != 1 || len(page.Docs) != 1 {
		t.Fatal("document listing rejected v4", page, code, err)
	}
	store := &datasetIDStoreProbe{}
	chunks := service.NewChunkService(store)
	scope := &service.GetChunkRequest{DatasetID: id, DocumentID: docID, ChunkID: "controlled"}
	if _, err := chunks.Get(t.Context(), scope, "dataset-owner"); !errors.Is(err, errDatasetStoreBoundary) || store.reads != 1 {
		t.Fatal("authorized chunk did not reach external boundary", err, store.reads)
	}
	if _, _, err := datasets.FlattenMetadata(t.Context(), []string{id}, "dataset-owner", store); err == nil || store.searches != 1 {
		t.Fatal("authorized metadata did not reach external boundary", err, store.searches)
	}
	snapshot := func() string {
		t.Helper()
		var raw string
		if err := read.QueryRow("SELECT COALESCE(jsonb_agg(to_jsonb(k) ORDER BY id)::text,'[]') FROM t_ai_knowledgebases k").Scan(&raw); err != nil {
			t.Fatal(err)
		}
		return raw
	}
	before := snapshot()
	denied := func(label string, code common.ErrorCode, err error) {
		t.Helper()
		if err == nil || code == common.CodeSuccess {
			t.Fatal("foreign tenant accepted", label, code, err)
		}
	}
	_, code, err = datasets.GetDataset(t.Context(), id, "dataset-other")
	denied("GET", code, err)
	_, _, code, err = datasets.ListDatasets(id, "", 1, 30, "create_time", true, "", nil, "", "dataset-other")
	denied("list", code, err)
	_, code, err = kb.UpdateKB(&service.UpdateKBRequest{KBID: id, Name: "forbidden", ParserID: "naive"}, "dataset-other")
	denied("update", code, err)
	_, code, err = datasets.UpdateMetadataConfig(t.Context(), id, "dataset-other", service.MetadataConfigRequest{Enabled: &enabled})
	denied("metadata update", code, err)
	_, code, err = datasets.GetMetadataConfig(t.Context(), id, "dataset-other")
	denied("metadata GET", code, err)
	_, code, err = documents.ListDatasetDocuments(t.Context(), id, "dataset-other", 1, 30, "", true)
	denied("document list", code, err)
	_, code, err = datasets.FlattenMetadata(t.Context(), []string{id}, "dataset-other", store)
	denied("flatten metadata", code, err)
	if _, err := chunks.Get(t.Context(), scope, "dataset-other"); !errors.Is(err, service.ErrDatasetAccessDenied) {
		t.Fatal("foreign chunk access", err)
	}
	if store.reads != 1 || store.searches != 1 || snapshot() != before {
		t.Fatal("denied calls changed committed state or reached external store")
	}
	journal.Event("v4-created-read-updated-denied-readback", map[string]any{"dataset_id": id, "generated_version": 4, "sql_readback": true, "foreign_sql_unchanged": true, "foreign_store_calls": 0, "controlled_store_boundary": fmt.Sprint(errDatasetStoreBoundary)})
}
