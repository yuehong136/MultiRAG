package handler

import (
	"net/http"
	"path/filepath"
	"runtime"
	"testing"

	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/server"
	"multirag/internal/service"
)

func (f *canonicalHTTPFixture) documentEngine(t *testing.T) engine.DocEngine {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	t.Chdir(filepath.Join(filepath.Dir(file), "..", ".."))

	if err := engine.Init(&server.DocEngineConfig{Type: server.EngineElasticsearch, ES: &f.Config.Elasticsearch}); err != nil {
		t.Fatal(err)
	}
	store := engine.Get()
	if store == nil {
		t.Fatal("canonical ES engine missing")
	}
	index := "multirag_" + f.ids["owner"]
	metadata := "multirag_doc_meta_" + f.ids["owner"]
	f.Journal.Event("reserve-canonical-handler-indices", map[string]any{"indices": []string{index, metadata}, "host": f.Config.Elasticsearch.Hosts})
	t.Cleanup(func() {
		for _, name := range []string{index, metadata} {
			request, err := http.NewRequest(http.MethodDelete, f.Config.Elasticsearch.Hosts+"/"+name, nil)
			if err != nil {
				t.Error(err)
				continue
			}
			response, err := http.DefaultClient.Do(request)
			if err != nil {
				t.Error(err)
				continue
			}
			response.Body.Close()
			request, _ = http.NewRequest(http.MethodHead, f.Config.Elasticsearch.Hosts+"/"+name, nil)
			response, err = http.DefaultClient.Do(request)
			absent := err == nil && response.StatusCode == 404
			if response != nil {
				response.Body.Close()
			}
			f.Journal.Event("cleanup-canonical-handler-index", map[string]any{"index": name, "absent": absent})
			if !absent {
				t.Error("independent handler index cleanup", name, err)
			}
		}
	})
	return store
}
func TestDatasetGraphHTTPAndSQL(t *testing.T) {
	f := newCanonicalHTTPFixture(t)
	store := f.documentEngine(t)
	index := "multirag_" + f.ids["owner"]
	kb, neighbor := f.datasets["shared"], f.datasets["private"]
	for _, dataset := range []string{kb, neighbor} {
		if err := store.CreateChunkStore(t.Context(), index, dataset, 2, "naive"); err != nil {
			t.Fatal(err)
		}
	}
	for _, row := range []map[string]any{{"id": "graph", "kb_id": kb, "doc_id": "graphdoc", "knowledge_graph_kwd": "graph", "content_with_weight": `{"nodes":[{"id":"a","pagerank":0},{"id":"b","pagerank":2}],"edges":[{"source":"a","target":"b","weight":1},{"source":"a","target":"a","weight":10}]}`}, {"id": "normal", "kb_id": kb, "doc_id": "normaldoc", "content_with_weight": "must remain"}, {"id": "neighbor", "kb_id": neighbor, "doc_id": "neighbor", "knowledge_graph_kwd": "graph", "content_with_weight": `{"nodes":[{"id":"other"}],"edges":[]}`}} {
		if failed, err := store.InsertChunks(t.Context(), []map[string]any{row}, index, row["kb_id"].(string)); err != nil || len(failed) > 0 {
			t.Fatal(failed, err)
		}
	}
	h := NewDatasetsHandler(service.NewDatasetService(), nil)
	app := f.app()
	app.GET("/datasets/:dataset_id/graph", h.GetKnowledgeGraph)
	app.DELETE("/datasets/:dataset_id/graph", h.DeleteKnowledgeGraph)
	url := f.serve(t, app)
	for _, actor := range []string{"owner", "member"} {
		_, result, _, _ := f.request(t, url, actor, "GET", "/datasets/"+kb+"/graph", nil)
		if result["code"] != float64(0) {
			t.Fatal("authorized graph", actor, result)
		}
		graph := result["data"].(map[string]any)["graph"].(map[string]any)
		nodes, edges := graph["nodes"].([]any), graph["edges"].([]any)
		if len(nodes) != 2 || len(edges) != 1 || nodes[0].(map[string]any)["id"] != "b" {
			t.Fatal("real graph sort/reference filtering", graph)
		}
	}
	for _, actor := range []string{"", "invite", "revoked", "foreign", "disabled"} {
		for _, method := range []string{"GET", "DELETE"} {
			_, result, _, _ := f.request(t, url, actor, method, "/datasets/"+kb+"/graph", nil)
			if result["code"] == float64(0) || result["data"] != nil {
				t.Fatal("unauthorized graph", actor, method, result)
			}
		}
	}
	before, err := store.Search(t.Context(), &types.SearchRequest{IndexNames: []string{index}, KbIDs: []string{kb}, SelectFields: []string{"id"}, Limit: 10})
	if err != nil || len(before.Chunks) != 2 {
		t.Fatal("denied graph deletion changed index", before, err)
	}
	_, deleted, _, _ := f.request(t, url, "owner", "DELETE", "/datasets/"+kb+"/graph", nil)
	if deleted["code"] != float64(0) {
		t.Fatal("authorized graph delete", deleted)
	}
	for _, dataset := range []string{kb, neighbor} {
		got, err := store.Search(t.Context(), &types.SearchRequest{IndexNames: []string{index}, KbIDs: []string{dataset}, SelectFields: []string{"id", "content_with_weight"}, Limit: 10})
		if err != nil || len(got.Chunks) != 1 {
			t.Fatal("graph removal crossed KB or normal chunk", dataset, got, err)
		}
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM knowledgebase WHERE id=?", kb).Scan(&count); err != nil || count != 1 {
		t.Fatal("graph SQL readback", count, err)
	}
	f.Journal.Event("canonical-graph-http-mysql-es-readback", map[string]any{"actual_es_http_sql": true, "authorized_sort_reference_filter": true, "denied_no_delete": true, "graph_delete_keeps_normal_neighbor": true})
}
