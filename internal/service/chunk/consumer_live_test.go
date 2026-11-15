package chunk

import (
	"context"
	"errors"
	"net/http"
	"path/filepath"
	"runtime"
	"sync/atomic"
	"testing"

	"multirag/internal/dao"
	"multirag/internal/engine"
	esengine "multirag/internal/engine/elasticsearch"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/service"
	"multirag/internal/testsupport/scratch"
)

type canonicalAccessEngine struct {
	engine.DocEngine
	calls   atomic.Int64
	failure error
}

func (e *canonicalAccessEngine) GetChunk(ctx context.Context, index, id string, kbs []string) (any, error) {
	e.calls.Add(1)
	if e.failure != nil {
		return nil, e.failure
	}
	return e.DocEngine.GetChunk(ctx, index, id, kbs)
}
func (e *canonicalAccessEngine) Search(ctx context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	e.calls.Add(1)
	return e.DocEngine.Search(ctx, req)
}
func (e *canonicalAccessEngine) UpdateChunks(ctx context.Context, condition, value map[string]any, index, kb string) error {
	e.calls.Add(1)
	return e.DocEngine.UpdateChunks(ctx, condition, value, index, kb)
}
func (e *canonicalAccessEngine) DeleteChunks(ctx context.Context, condition map[string]any, index, kb string) (int64, error) {
	e.calls.Add(1)
	return e.DocEngine.DeleteChunks(ctx, condition, index, kb)
}
func TestCanonicalChunkAccessBeforeEngineIO(t *testing.T) {
	f := scratch.New(t)
	previous := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = previous })
	_, file, _, _ := runtime.Caller(0)
	t.Chdir(filepath.Join(filepath.Dir(file), "..", "..", ".."))
	if err := f.DB.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.Knowledgebase{}, &entity.Document{}); err != nil {
		t.Fatal(err)
	}
	owner, member, invite, revoked, disabled := f.Prefix+"owner", f.Prefix+"member", f.Prefix+"invite", f.Prefix+"revoked", f.Prefix+"disabled"
	active := "1"
	for _, actor := range []string{owner, member, invite, revoked, disabled} {
		status := "1"
		if actor == disabled {
			status = "0"
		}
		if err := f.DB.Create(&entity.User{ID: actor, Email: actor + "@fixture.invalid", Nickname: actor, IsActive: status, Status: &status}).Error; err != nil {
			t.Fatal(err)
		}
		role, state := "normal", "1"
		if actor == owner {
			role = "owner"
		}
		if actor == invite {
			role = "invite"
		}
		if actor == revoked {
			state = "0"
		}
		if err := f.DB.Create(&entity.UserTenant{ID: actor, UserID: actor, TenantID: owner, Role: role, Status: &state}).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := f.DB.Create(&entity.Tenant{ID: owner, Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	shared, private, doc, privateDoc := f.Prefix+"shared", f.Prefix+"private", f.Prefix+"doc", f.Prefix+"privateDoc"
	for _, row := range []entity.Knowledgebase{{ID: shared, TenantID: owner, CreatedBy: owner, Name: "shared", Permission: "team", Status: &active, ParserConfig: entity.JSONMap{}}, {ID: private, TenantID: owner, CreatedBy: owner, Name: "private", Permission: "me", Status: &active, ParserConfig: entity.JSONMap{}}} {
		if err := f.DB.Create(&row).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, row := range []entity.Document{{ID: doc, KbID: shared, CreatedBy: owner, ParserID: "naive", ParserConfig: entity.JSONMap{}, Status: &active}, {ID: privateDoc, KbID: private, CreatedBy: owner, ParserID: "naive", ParserConfig: entity.JSONMap{}, Status: &active}} {
		if err := f.DB.Create(&row).Error; err != nil {
			t.Fatal(err)
		}
	}
	index := "multirag_" + owner
	f.Journal.Event("reserve-chunk-access-index", map[string]any{"index": index})
	store, err := esengine.NewEngine(&f.Config.Elasticsearch)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		store.Close()
		req, _ := http.NewRequest(http.MethodDelete, f.Config.Elasticsearch.Hosts+"/"+index, nil)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Error(err)
			return
		}
		resp.Body.Close()
		req, _ = http.NewRequest(http.MethodHead, f.Config.Elasticsearch.Hosts+"/"+index, nil)
		resp, err = http.DefaultClient.Do(req)
		absent := err == nil && resp.StatusCode == 404
		if resp != nil {
			resp.Body.Close()
		}
		f.Journal.Event("cleanup-chunk-access-index", map[string]any{"index": index, "absent": absent})
		if !absent {
			t.Error("index cleanup readback", err)
		}
	})
	if err := store.CreateChunkStore(t.Context(), index, shared, 2, "naive"); err != nil {
		t.Fatal(err)
	}
	if failed, err := store.InsertChunks(t.Context(), []map[string]any{{"id": "selected", "doc_id": doc, "content_with_weight": "source", "available_int": 1, "q_2_vec": []float64{1, 0}}}, index, shared); err != nil || len(failed) > 0 {
		t.Fatal(failed, err)
	}
	counted := &canonicalAccessEngine{DocEngine: store}
	svc := &ChunkService{docEngine: counted, kbDAO: dao.NewKnowledgebaseDAO(), userTenantDAO: dao.NewUserTenantDAO(), documentDAO: dao.NewDocumentDAO()}
	if result, err := svc.Get(&service.GetChunkRequest{ChunkID: "selected", DatasetID: shared, DocumentID: doc}, member); err != nil || result == nil || counted.calls.Load() != 1 {
		t.Fatal("positive control did not reach real ES", result, err, counted.calls.Load())
	}
	for _, actor := range []string{invite, revoked, disabled, "missing"} {
		before := counted.calls.Load()
		if _, err := svc.Get(&service.GetChunkRequest{ChunkID: "selected", DatasetID: shared, DocumentID: doc}, actor); err == nil {
			t.Fatal("unauthorized Get", actor)
		}
		if _, err := svc.List(&service.ListChunksRequest{DocID: doc}, actor); err == nil {
			t.Fatal("unauthorized List", actor)
		}
		if err := svc.UpdateChunk(&service.UpdateChunkRequest{DatasetID: shared, DocumentID: doc, ChunkID: "selected"}, actor); err == nil {
			t.Fatal("unauthorized Update", actor)
		}
		if _, err := svc.RemoveChunks(&service.RemoveChunksRequest{DatasetID: shared, DocID: doc, DeleteAll: true}, actor); err == nil {
			t.Fatal("unauthorized Remove", actor)
		}
		if _, err := svc.RetrievalTest(&service.RetrievalTestRequest{Datasets: []string{shared}, Question: "source"}, actor); err == nil {
			t.Fatal("unauthorized Retrieval", actor)
		}
		if counted.calls.Load() != before {
			t.Fatal("denied operation performed ES IO", actor, counted.calls.Load()-before)
		}
	}
	before := counted.calls.Load()
	if _, err := svc.Get(&service.GetChunkRequest{ChunkID: "private", DatasetID: private, DocumentID: privateDoc}, member); err == nil {
		t.Fatal("private Get")
	}
	if _, err := svc.List(&service.ListChunksRequest{DocID: privateDoc}, member); err == nil {
		t.Fatal("private List")
	}
	if err := svc.UpdateChunk(&service.UpdateChunkRequest{DatasetID: private, DocumentID: privateDoc, ChunkID: "private"}, member); err == nil {
		t.Fatal("private Update")
	}
	if _, err := svc.RemoveChunks(&service.RemoveChunksRequest{DatasetID: private, DocID: privateDoc, DeleteAll: true}, member); err == nil {
		t.Fatal("private Remove")
	}
	if counted.calls.Load() != before {
		t.Fatal("private denial performed ES IO")
	}
	counted.failure = errors.New("controlled engine failure")
	if result, err := svc.Get(&service.GetChunkRequest{ChunkID: "selected", DatasetID: shared, DocumentID: doc}, owner); !errors.Is(err, counted.failure) || result != nil {
		t.Fatal("original Get swallowed engine failure", result, err)
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM document").Scan(&count); err != nil || count != 2 {
		t.Fatal("denied operations mutated SQL", count, err)
	}
	f.Journal.Event("canonical-chunk-access-zero-io", map[string]any{"positive_control_real_es_read": true, "invite_revoked_disabled_missing_private_zero_engine_calls": true, "get_engine_failure_preserved": true, "independent_sql_readback": true})
}
