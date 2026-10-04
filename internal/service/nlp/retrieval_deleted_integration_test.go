package nlp

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	infinitySDK "github.com/infiniflow/infinity-go-sdk"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"

	"multirag/internal/dao"
	"multirag/internal/engine/infinity"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/logger"
	"multirag/internal/server"
	"multirag/internal/utility"
)

// The runner owns the SQL database; all index data lives in a random Infinity DB.
func TestRetrievalScratchPostgresInfinity(t *testing.T) {
	dsn, uri := os.Getenv("MULTIRAG_GO_RETRIEVAL_DSN"), os.Getenv("MULTIRAG_GO_RETRIEVAL_INFINITY_URI")
	if dsn == "" || uri == "" {
		t.Skip("requires owned retrieval SQL scratch and Infinity URI")
	}
	u, err := url.Parse(dsn)
	if err != nil || !strings.HasPrefix(strings.TrimPrefix(u.Path, "/"), "multirag_go_retrieval_") {
		t.Fatal("SQL scratch DB not owned")
	}
	probe, err := net.DialTimeout("tcp", uri, 2*time.Second)
	if err != nil {
		t.Fatal("Infinity unavailable")
	}
	probe.Close()
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: gormlogger.Default.LogMode(gormlogger.Silent)})
	if err != nil {
		t.Fatal("SQL scratch connection failed")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	old := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = old })
	if err := db.AutoMigrate(&entity.Document{}, &entity.Knowledgebase{}); err != nil {
		t.Fatal(err)
	}
	valid, disabled := "1", "0"
	if err := db.Create(&entity.Knowledgebase{ID: "kb", TenantID: "tenant", Name: "scratch", CreatedBy: "user", Status: &valid}).Error; err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"live", "gone", "disabled"} {
		status := &valid
		if id == "disabled" {
			status = &disabled
		}
		if err := db.Create(&entity.Document{ID: id, KbID: "kb", ParserID: "naive", ParserConfig: entity.JSONMap{}, Type: "txt", Suffix: "txt", CreatedBy: "user", Status: status}).Error; err != nil {
			t.Fatal(err)
		}
	}
	docs := dao.NewDocumentDAO()
	if got, err := docs.GetByIDs(context.Background(), nil); err != nil || len(got) != 0 {
		t.Fatalf("empty lookup: %v %v", got, err)
	}
	// Independent native SQL read confirms the disabled row exists and is not deleted.
	var status string
	if err := pool.QueryRow(`SELECT status FROM t_ai_documents WHERE id='disabled'`).Scan(&status); err != nil || status != "0" {
		t.Fatalf("disabled row: %q %v", status, err)
	}
	rows, err := docs.GetByIDs(context.Background(), []string{"live", "live", "disabled", "missing"})
	if err != nil || len(rows) != 2 {
		t.Fatalf("document existence: %v %v", rows, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := docs.GetByIDs(ctx, []string{"live"}); !errors.Is(err, context.Canceled) {
		t.Fatalf("SQL cancellation lost: %v", err)
	}
	t.Chdir(utility.GetProjectRoot())
	if err := logger.Init("error"); err != nil {
		t.Fatal(err)
	}
	indexDB := "multirag_go_retrieval_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	index, err := infinity.NewEngine(&server.InfinityConfig{URI: uri, DBName: indexDB})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		parts := strings.Split(uri, ":")
		var port int
		if len(parts) != 2 {
			t.Error("invalid cleanup URI")
			return
		}
		if _, err := fmt.Sscan(parts[1], &port); err != nil {
			t.Error(err)
			return
		}
		conn, err := infinitySDK.Connect(infinitySDK.NetworkAddress{IP: parts[0], Port: port})
		if err != nil {
			t.Error(err)
			return
		}
		defer conn.Disconnect()
		if _, err := conn.DropDatabase(indexDB, infinitySDK.ConflictTypeError); err != nil {
			t.Error(err)
		}
		if _, err := conn.GetDatabase(indexDB); err == nil {
			t.Error("Infinity scratch DB survived cleanup")
		}
		if err := index.Close(); err != nil {
			t.Error(err)
		}
	})
	if err := index.CreateDataset(context.Background(), "multirag_tenant", "kb", 2, "naive"); err != nil {
		t.Fatal(err)
	}
	chunks := []map[string]interface{}{}
	for _, id := range []string{"live", "gone", "disabled", "graph_raptor_x"} {
		chunk := map[string]interface{}{"id": "chunk-" + id, "doc_id": id, "kb_id": "kb", "content": "question content " + id, "docnm": id, "available_int": 1, "q_2_vec": []float64{1, 0}}
		if id == "graph_raptor_x" {
			chunk["raptor_kwd"] = "raptor"
		}
		if id == "live" {
			chunk["mom_id"] = "chunk-gone"
		}
		chunks = append(chunks, chunk)
	}
	if failed, err := index.InsertDataset(context.Background(), chunks, "multirag_tenant", "kb"); err != nil || len(failed) > 0 {
		t.Fatalf("index insert failed: %v %v", failed, err)
	}
	if _, err := pool.Exec(`DELETE FROM t_ai_documents WHERE id='gone'`); err != nil {
		t.Fatal(err)
	}
	var count int
	if err := pool.QueryRow(`SELECT count(*) FROM t_ai_documents WHERE id='gone'`).Scan(&count); err != nil || count != 0 {
		t.Fatalf("deletion readback: %d %v", count, err)
	}
	svc := NewRetrievalService(index)
	search := &RetrievalSearchRequest{TenantIDs: []string{"tenant"}, KbIDs: []string{"kb"}, Page: 1, PageSize: 10, Top: 10}
	result, err := svc.Search(context.Background(), search)
	if err != nil || len(result.IDs) != 4 {
		t.Fatalf("residual index read: %v %v", result, err)
	}
	if chunkScalar(result.Field["chunk-graph_raptor_x"]["raptor_kwd"]) != "raptor" {
		t.Fatal("Infinity projection lost RAPTOR marker")
	}
	result, err = svc.pruneDeletedChunks(context.Background(), result, search.KbIDs)
	if err != nil || len(result.IDs) != 3 || result.Total != 4 || result.Field["chunk-gone"] != nil || result.Field["chunk-disabled"] == nil || result.Field["chunk-graph_raptor_x"] == nil {
		t.Fatalf("SQL/index lifetime mismatch: %v %v", result, err)
	}
	if err := InitQueryBuilder(filepath.Join(utility.GetProjectRoot(), "resource", "wordnet")); err != nil {
		t.Fatal(err)
	}
	ranker := &retrievalRerankDriver{}
	weight, highlight := 1.0, true
	retrieved, err := svc.Retrieval(context.Background(), &RetrievalRequest{
		TenantIDs: search.TenantIDs, KbIDs: search.KbIDs, Question: "question", Page: 1, PageSize: 10,
		VectorSimilarityWeight: &weight, Highlight: &highlight, RankFeature: &map[string]float64{},
		RerankModel: models.NewRerankModel(ranker, nil, nil),
	})
	if err != nil || len(retrieved.Chunks) != 3 || len(ranker.calls) != 1 || len(ranker.calls[0]) != 3 {
		t.Fatalf("real index retrieval did not prune before ranking: %v %v", retrieved, err)
	}
	for _, text := range ranker.calls[0] {
		if strings.Contains(text, "gone") {
			t.Fatal("deleted index text reached reranker")
		}
	}
	// Follow the actual ChunkService consumer's parent-expansion step against
	// the residual index row, whose SQL document was physically deleted above.
	expanded := RetrievalByChildren(retrieved.Chunks, search.TenantIDs, index, context.Background())
	if len(expanded) != 3 {
		t.Fatalf("parent expansion lost verified children: %v", expanded)
	}
	for _, chunk := range expanded {
		if chunkScalar(chunk["doc_id"]) == "gone" || strings.Contains(fmt.Sprint(chunk["content_with_weight"]), "gone") {
			t.Fatal("consumer parent expansion reintroduced deleted text")
		}
	}
	// Keep index unchanged, delete source SQL row and soft-delete the dataset.
	if _, err := pool.Exec(`DELETE FROM t_ai_documents WHERE id='live'`); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(`UPDATE t_ai_knowledgebases SET status='0' WHERE id='kb'`); err != nil {
		t.Fatal(err)
	}
	result, err = svc.Search(context.Background(), search)
	if err != nil {
		t.Fatal(err)
	}
	result, err = svc.pruneDeletedChunks(context.Background(), result, search.KbIDs)
	if err != nil || !reflect.DeepEqual(result.IDs, []string{"chunk-disabled"}) || result.Total != 4 {
		t.Fatalf("second committed deletion not observed: %v %v", result, err)
	}
	// Explicit availability filtering remains the engine's responsibility.
	result, err = svc.Search(context.Background(), &RetrievalSearchRequest{TenantIDs: search.TenantIDs, KbIDs: search.KbIDs, Page: 1, PageSize: 10, Filter: map[string]interface{}{"available_int": 0}})
	if err != nil || len(result.IDs) != 0 {
		t.Fatalf("availability predicate changed: %v %v", result, err)
	}
	indexed, err := index.Search(context.Background(), &types.SearchRequest{IndexNames: []string{"multirag_tenant"}, KbIDs: []string{"kb"}, Limit: 10})
	if err != nil || len(indexed.Chunks) != 4 {
		t.Fatalf("retrieval mutated index: %v %v", indexed, err)
	}
}
