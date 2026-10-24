package engine_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"multirag/internal/engine"
	esengine "multirag/internal/engine/elasticsearch"
	infengine "multirag/internal/engine/infinity"
	"multirag/internal/engine/types"
	"multirag/internal/server"
)

type engineStoreLiveConfig struct {
	Tenant   string
	Database server.DatabaseConfig
	ES       server.ElasticsearchConfig
	Infinity server.InfinityConfig
}

func TestEngineStoresLive(t *testing.T) {
	config := os.Getenv("MULTIRAG_ENGINE_LIVE_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned engine live config required")
		}
		t.Skip("requires owned engine scratch resources")
	}
	raw, err := os.ReadFile(config)
	if err != nil {
		t.Fatal(err)
	}
	var cfg engineStoreLiveConfig
	if err = json.Unmarshal(raw, &cfg); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(cfg.Tenant, "multirag_afterd7_b_") || cfg.Infinity.DBName != cfg.Tenant {
		t.Fatal("unowned engine fixture")
	}
	t.Chdir(filepath.Join("..", ".."))
	kind := os.Getenv("MULTIRAG_ENGINE_LIVE_KIND")
	var store engine.DocEngine
	switch kind {
	case "elasticsearch":
		store, err = esengine.NewEngine(&cfg.ES)
	case "infinity":
		store, err = infengine.NewEngine(&cfg.Infinity)
	default:
		t.Fatal("explicit engine kind required")
	}
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	ctx := t.Context()
	base := "multirag_" + cfg.Tenant
	kbA, kbB := "store_a", "store_b"
	foreign := "multirag_" + cfg.Tenant + "_foreign"
	for _, target := range []struct{ base, kb string }{{base, kbA}, {base, kbB}, {foreign, kbA}} {
		if err := store.CreateChunkStore(ctx, target.base, target.kb, 2, "naive"); err != nil {
			t.Fatal(err)
		}
		exists, err := store.ChunkStoreExists(ctx, target.base, target.kb)
		if err != nil || !exists {
			t.Fatal(exists, err)
		}
	}
	insert := func(index, kb, id, content string) {
		t.Helper()
		if failed, err := store.InsertChunks(ctx, []map[string]interface{}{{"id": id, "kb_id": kb, "doc_id": "doc", "content_with_weight": content, "available_int": 1, "q_2_vec": []float64{0, 1}, "tag_kwd": []string{"keep", "remove"}}}, index, kb); err != nil || len(failed) > 0 {
			t.Fatal(failed, err)
		}
	}
	insert(base, kbA, "O'Brien", "first")
	insert(base, kbB, "neighbor", "second")
	insert(foreign, kbA, "foreign", "foreign")
	query := func(index, kb string) []map[string]interface{} {
		t.Helper()
		result, err := store.Search(ctx, &types.SearchRequest{IndexNames: []string{index}, KbIDs: []string{kb}, SelectFields: []string{"id", "kb_id", "content_with_weight", "tag_kwd", "available_int"}, Limit: 30})
		if err != nil {
			t.Fatal(err)
		}
		return result.Chunks
	}
	if err := store.UpdateChunks(ctx, map[string]interface{}{"id": []string{"O'Brien"}}, map[string]interface{}{"content_with_weight": "updated"}, base, kbA); err != nil {
		t.Fatal(err)
	}
	rows := query(base, kbA)
	if len(rows) != 1 || rows[0]["content_with_weight"] != "updated" {
		t.Fatal("typed-id update failed", rows)
	}
	if err := store.UpdateChunks(ctx, map[string]interface{}{"id": []string{"O'Brien"}}, map[string]interface{}{"remove": map[string]interface{}{"tag_kwd": "remove"}}, base, kbA); err != nil {
		t.Fatal("quoted remove id", err)
	}
	rows = query(base, kbA)
	tag := fmt.Sprint(rows[0]["tag_kwd"])
	if strings.Contains(tag, "remove") || !strings.Contains(tag, "keep") {
		t.Fatal("remove failed", rows)
	}
	for _, bad := range []map[string]interface{}{{"id": []string{}}, {"id": nil}, {"unsupported": struct{}{}}} {
		if err := store.UpdateChunks(ctx, bad, map[string]interface{}{"available_int": 0}, base, kbA); err == nil {
			t.Fatal("unbounded update accepted", bad)
		}
		if _, err := store.DeleteChunks(ctx, bad, base, kbA); err == nil {
			t.Fatal("unbounded delete accepted", bad)
		}
	}
	if len(query(base, kbA)) != 1 || len(query(base, kbB)) != 1 || len(query(foreign, kbA)) != 1 {
		t.Fatal("invalid operations affected rows")
	}

	if kind == "infinity" {
		for i, kb := range []string{kbA, kbB} {
			content := ""
			image := "filled"
			if i == 0 {
				content = "first nonempty"
				image = ""
			}
			if failed, err := store.InsertChunks(ctx, []map[string]interface{}{{"id": "merge-parent", "kb_id": kb, "doc_id": "doc", "content_with_weight": content, "img_id": image, "available_int": 1, "q_2_vec": []float64{0, 1}}}, base, kb); err != nil || len(failed) > 0 {
				t.Fatal(failed, err)
			}
		}
		parent, err := store.GetChunk(ctx, base, "merge-parent", []string{kbA, kbB})
		if err != nil {
			t.Fatal(err)
		}
		chunk, ok := parent.(map[string]interface{})
		if !ok || chunk["content_with_weight"] != "first nonempty" || chunk["img_id"] != "filled" {
			t.Fatal("parent merge lost first nonempty or failed to fill missing", parent)
		}
		for _, kb := range []string{kbA, kbB} {
			if count, err := store.DeleteChunks(ctx, map[string]interface{}{"id": []string{"merge-parent"}}, base, kb); err != nil || count != 1 {
				t.Fatal(count, err)
			}
		}
	}
	if err := store.CreateMetadataStore(ctx, cfg.Tenant); err != nil {
		t.Fatal(err)
	}
	for _, meta := range []map[string]interface{}{{"id": "meta'one", "kb_id": kbA, "meta_fields": map[string]interface{}{"keep": 1, "replace": "old"}}, {"id": "meta-two", "kb_id": kbB, "meta_fields": map[string]interface{}{"neighbor": 2}}} {
		if failed, err := store.InsertMetadata(ctx, []map[string]interface{}{meta}, cfg.Tenant); err != nil || len(failed) > 0 {
			t.Fatal(failed, err)
		}
	}

	if kind == "elasticsearch" {
		legacy := map[string]interface{}{"id": "meta'legacy", "kb_id": kbA, "meta_fields": map[string]interface{}{"preserved": true}}
		raw, _ := json.Marshal(legacy)
		request, err := http.NewRequest("POST", cfg.ES.Hosts+"/multirag_doc_meta_"+cfg.Tenant+"/_doc/python-physical?refresh=wait_for", bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		request.SetBasicAuth(cfg.ES.Username, cfg.ES.Password)
		request.Header.Set("Content-Type", "application/json")
		response, err := http.DefaultClient.Do(request)
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Close()
		if response.StatusCode != 201 {
			t.Fatal("native Python-shape seed failed", response.StatusCode)
		}
		if err := store.UpdateMetadata(ctx, "meta'legacy", kbA, map[string]interface{}{"zero": 0}, cfg.Tenant); err != nil {
			t.Fatal(err)
		}
		result, err := store.Search(ctx, &types.SearchRequest{IndexNames: []string{"multirag_doc_meta_" + cfg.Tenant}, KbIDs: []string{kbA}, Filter: map[string]interface{}{"id": "meta'legacy"}, SelectFields: []string{"id", "kb_id", "meta_fields"}, Limit: 5})
		if err != nil || len(result.Chunks) != 1 {
			t.Fatal("metadata update duplicated Python row", result, err)
		}
		fields := result.Chunks[0]["meta_fields"].(map[string]interface{})
		if fields["preserved"] != true || fields["zero"] != float64(0) {
			t.Fatal(fields)
		}
		if count, err := store.DeleteMetadata(ctx, map[string]interface{}{"id": "meta'legacy", "kb_id": kbA}, cfg.Tenant); err != nil || count != 1 {
			t.Fatal(count, err)
		}
	}
	if err := store.UpdateMetadata(ctx, "meta'one", kbA, map[string]interface{}{"replace": "new", "zero": 0}, cfg.Tenant); err != nil {
		t.Fatal(err)
	}
	result, err := store.Search(ctx, &types.SearchRequest{IndexNames: []string{"multirag_doc_meta_" + cfg.Tenant}, KbIDs: []string{kbA}, Filter: map[string]interface{}{"id": []string{"meta'one"}}, SelectFields: []string{"id", "kb_id", "meta_fields"}, Limit: 2})
	if err != nil || len(result.Chunks) != 1 {
		t.Fatal(result, err)
	}
	fields := result.Chunks[0]["meta_fields"]
	var decoded map[string]interface{}
	switch v := fields.(type) {
	case map[string]interface{}:
		decoded = v
	case string:
		if err = json.Unmarshal([]byte(v), &decoded); err != nil {
			t.Fatal(err)
		}
	default:
		t.Fatalf("invalid metadata %T", fields)
	}
	if decoded["keep"] != float64(1) || decoded["replace"] != "new" || decoded["zero"] != float64(0) {
		t.Fatal("metadata merge lost fields", decoded)
	}
	if count, err := store.DeleteMetadata(ctx, map[string]interface{}{"id": []string{"meta'one"}, "kb_id": kbA}, cfg.Tenant); err != nil || count != 1 {
		t.Fatal(count, err)
	}
	result, err = store.Search(ctx, &types.SearchRequest{IndexNames: []string{"multirag_doc_meta_" + cfg.Tenant}, KbIDs: []string{kbB}, SelectFields: []string{"id", "kb_id", "meta_fields"}, Limit: 5})
	if err != nil || len(result.Chunks) != 1 {
		t.Fatal("neighbor metadata deleted", result, err)
	}
	if err := store.DropChunkStore(ctx, base, kbA); err != nil {
		t.Fatal(err)
	}
	if rows := query(base, kbB); len(rows) != 1 {
		t.Fatal("dataset drop deleted neighbor", rows)
	}
	if len(query(foreign, kbA)) != 1 {
		t.Fatal("foreign tenant changed")
	}
	if err := store.DropMetadataStore(ctx, cfg.Tenant); err != nil {
		t.Fatal(err)
	}
	if exists, err := store.MetadataStoreExists(ctx, cfg.Tenant); err != nil || exists {
		t.Fatal(exists, err)
	}
	t.Log("real engine create/insert/update/remove/delete, typed/quoted IDs, metadata merge and KB/tenant isolation passed")
}
