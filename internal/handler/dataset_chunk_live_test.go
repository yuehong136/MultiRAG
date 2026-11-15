package handler

import (
	"fmt"
	"net/url"
	"strconv"
	"testing"

	"multirag/internal/cli"
	"multirag/internal/engine/types"
	"multirag/internal/service"
	chunkservice "multirag/internal/service/chunk"
)

func TestDatasetChunkMetadataCLIAndStores(t *testing.T) {
	f := newCanonicalHTTPFixture(t)
	store := f.documentEngine(t)
	index := "multirag_" + f.ids["owner"]
	kb, private := f.datasets["shared"], f.datasets["private"]
	doc := f.document(t, "shared", "visible.txt")
	otherdoc := f.document(t, "shared", "other.txt")
	privatedoc := f.document(t, "private", "private.txt")
	for _, dataset := range []string{kb, private} {
		if err := store.CreateChunkStore(t.Context(), index, dataset, 2, "naive"); err != nil {
			t.Fatal(err)
		}
	}
	for _, row := range []map[string]any{{"id": "selected", "kb_id": kb, "doc_id": doc, "docnm_kwd": "visible.txt", "content_with_weight": "selected source", "available_int": 1, "q_2_vec": []float64{0, 1}}, {"id": "other", "kb_id": kb, "doc_id": otherdoc, "content_with_weight": "other source", "available_int": 1, "q_2_vec": []float64{0, 1}}, {"id": "private", "kb_id": private, "doc_id": privatedoc, "content_with_weight": "private source", "available_int": 1, "q_2_vec": []float64{0, 1}}} {
		if failed, err := store.InsertChunks(t.Context(), []map[string]any{row}, index, row["kb_id"].(string)); err != nil || len(failed) > 0 {
			t.Fatal(failed, err)
		}
	}
	svc := chunkservice.NewChunkService()
	h := NewChunkHandler(svc, service.NewUserService())
	datasets := NewDatasetsHandler(service.NewDatasetService(), nil)
	app := f.app()
	app.GET("/api/v1/datasets/:dataset_id/documents/:document_id/chunks/:chunk_id", h.Get)
	app.DELETE("/api/v1/datasets/:dataset_id/documents/:document_id/chunks", h.RemoveChunks)
	app.POST("/api/v1/chunk/list", h.List)
	app.POST("/api/v1/chunk/update", h.UpdateChunk)
	app.GET("/api/v1/datasets", datasets.ListDatasets)
	urlString := f.serve(t, app)
	path := "/api/v1/datasets/" + kb + "/documents/" + doc + "/chunks/selected"
	for _, actor := range []string{"owner", "member"} {
		_, result, _, _ := f.request(t, urlString, actor, "GET", path, nil)
		if result["code"] != float64(0) || result["data"].(map[string]any)["content_with_weight"] != "selected source" {
			t.Fatal("active chunk get", actor, result)
		}
	}
	for _, actor := range []string{"", "invite", "revoked", "foreign", "disabled"} {
		for _, action := range []struct {
			method, path string
			body         any
		}{{"GET", path, nil}, {"POST", "/api/v1/chunk/list", map[string]any{"doc_id": doc}}, {"POST", "/api/v1/chunk/update", map[string]any{"dataset_id": kb, "document_id": doc, "chunk_id": "selected", "available": false}}, {"DELETE", "/api/v1/datasets/" + kb + "/documents/" + doc + "/chunks", map[string]any{"delete_all": true}}} {
			_, result, _, _ := f.request(t, urlString, actor, action.method, action.path, action.body)
			if result["code"] == float64(0) {
				t.Fatal("unauthorized chunk operation", actor, action.method, result)
			}
		}
	}
	for _, action := range []struct {
		method, path string
		body         any
	}{{"GET", "/api/v1/datasets/" + private + "/documents/" + privatedoc + "/chunks/private", nil}, {"POST", "/api/v1/chunk/list", map[string]any{"doc_id": privatedoc}}, {"POST", "/api/v1/chunk/update", map[string]any{"dataset_id": private, "document_id": privatedoc, "chunk_id": "private", "available": false}}, {"DELETE", "/api/v1/datasets/" + private + "/documents/" + privatedoc + "/chunks", map[string]any{"delete_all": true}}, {"GET", "/api/v1/datasets/" + private + "/documents/" + doc + "/chunks/selected", nil}, {"DELETE", "/api/v1/datasets/" + private + "/documents/" + doc + "/chunks", map[string]any{"delete_all": true}}} {
		_, result, _, _ := f.request(t, urlString, "member", action.method, action.path, action.body)
		if result["code"] == float64(0) {
			t.Fatal("private or substituted chunk scope accepted", action.path, result)
		}
	}
	// A real accessible document cannot be substituted for a different chunk's owner.
	_, mismatch, _, _ := f.request(t, urlString, "owner", "GET", "/api/v1/datasets/"+kb+"/documents/"+otherdoc+"/chunks/selected", nil)
	if mismatch["code"] == float64(0) {
		t.Fatal("chunk source document binding lost", mismatch)
	}
	_, listed, _, _ := f.request(t, urlString, "owner", "POST", "/api/v1/chunk/list", map[string]any{"doc_id": doc})
	if listed["code"] != float64(0) || listed["data"].(map[string]any)["total"] != float64(1) {
		t.Fatal("document chunk list broadened", listed)
	}
	_, updated, _, _ := f.request(t, urlString, "owner", "POST", "/api/v1/chunk/update", map[string]any{"dataset_id": kb, "document_id": doc, "chunk_id": "selected", "available": false})
	if updated["code"] != float64(0) {
		t.Fatal("explicit false update", updated)
	}
	actual, err := store.Search(t.Context(), &types.SearchRequest{IndexNames: []string{index}, KbIDs: []string{kb}, Filter: map[string]any{"id": "selected"}, SelectFields: []string{"available_int", "doc_id"}, Limit: 1})
	if err != nil || len(actual.Chunks) != 1 || fmt.Sprint(actual.Chunks[0]["available_int"]) != "0" {
		t.Fatal("false value lost", actual, err)
	}
	address, err := url.Parse(urlString)
	if err != nil {
		t.Fatal(err)
	}
	client := cli.NewHTTPClient()
	client.Host = address.Hostname()
	client.Port, _ = strconv.Atoi(address.Port())
	token := f.tokens["owner"]
	client.LoginToken = &token
	consumer := &cli.CLI{Config: &cli.CommandLineConfig{CLIMode: cli.APIMode, APIClientConfig: cli.APIModeConfig{CurrentAPIServer: "fixture", APIServerMap: map[string]*cli.APIServerConfig{"fixture": {Name: "fixture"}}}}, APIServerClientMap: map[string]*cli.HTTPClient{"fixture": client}}
	command, err := cli.NewParser("REMOVE CHUNKS 'selected' FROM DOCUMENT '" + doc + "' IN DATASET 'shared';").Parse(cli.APIMode)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := consumer.ExecuteCommand(command); err != nil {
		t.Fatal("real chunk remove CLI", err)
	}
	remaining, err := store.Search(t.Context(), &types.SearchRequest{IndexNames: []string{index}, KbIDs: []string{kb}, SelectFields: []string{"id", "doc_id"}, Limit: 10})
	if err != nil || len(remaining.Chunks) != 1 || remaining.Chunks[0]["id"] != "other" {
		t.Fatal("selected deletion crossed document", remaining, err)
	}
	f.Journal.Event("canonical-chunk-http-mysql-es-cli-readback", map[string]any{"active_chunk_subpackage": true, "denied_before_es_io": "verified by chunk/TestCanonicalChunkAccessBeforeEngineIO", "private_and_path_scope_bound": true, "false_update_preserved": true, "exact_remove_keeps_neighbor": true})
}
