package engine

import (
	"context"
	"encoding/json"
	"errors"
	"github.com/google/uuid"
	me "github.com/milvus-io/milvus/client/v2/entity"
	mc "github.com/milvus-io/milvus/client/v2/milvusclient"
	"multirag/internal/engine/types"
	"os"
	"strings"
	"testing"
	"time"
	"unicode/utf8"
)

func TestSkillFragmentsPreserveLargeUTF8AndMetadata(t *testing.T) {
	body := strings.Repeat("正文🍊orange\n", 400000)
	doc := map[string]interface{}{"skill_id": "orange", "folder_id": "folder", "description": strings.Repeat("说明", 20000), "tags": []string{"fruit"}, "content": body, "_search_text": body, "q_3_vec": []float64{1, 2, 3}}
	rows, head, err := skillDocumentFragments("orange", doc)
	if err != nil {
		t.Fatal(err)
	}
	texts := map[int64]string{}
	for _, row := range rows {
		if len(row.text) > 8192 || len(row.payload) > 8192 || !utf8.ValidString(row.text) || !utf8.ValidString(row.payload) {
			t.Fatal("invalid UTF8 fragment boundary")
		}
		if row.kind == "text" {
			texts[row.ordinal] = row.text
		}
	}
	var rebuilt strings.Builder
	for i := 0; i < len(texts); i++ {
		rebuilt.WriteString(texts[int64(i)])
	}
	if rebuilt.String() != body || head.kind != "head" || len(head.vector) != 3 {
		t.Fatal("fragment content changed")
	}
	if len(texts) < 600 {
		t.Fatalf("large document unexpectedly truncated: %d fragments", len(texts))
	}
}

func TestSkillPreparationFailurePreservesPublishedHead(t *testing.T) {
	rows, head, err := skillDocumentFragments("orange", map[string]interface{}{"_search_text": strings.Repeat("orange", 20000), "q_3_vec": []float64{1, 2, 3}})
	if err != nil {
		t.Fatal(err)
	}
	published := "previous-digest"
	writes := 0
	expected := errors.New("fragment readback unavailable")
	err = publishSkillDocument(rows, head, func(batch []skillFragment) error {
		writes++
		for _, row := range batch {
			if row.kind == "head" {
				published = row.digest
			}
		}
		return expected
	})
	if !errors.Is(err, expected) || published != "previous-digest" || writes != 1 {
		t.Fatal("failed preparation published a replacement")
	}
	err = publishSkillDocument(rows, head, func(batch []skillFragment) error {
		for _, row := range batch {
			if row.kind == "head" {
				published = row.digest
			}
		}
		return nil
	})
	if err != nil || published != head.digest {
		t.Fatal("verified preparation not published")
	}
}

// Real Milvus evidence for failure after fragment preparation but before head
// publication. The Go fixture provides only isolated infrastructure credentials.
func TestSkillMilvusLiveUnpublishedFragments(t *testing.T) {
	path := os.Getenv("MULTIRAG_SKILL_CORE_CONFIG")
	if path == "" {
		t.Skip("requires isolated fixture")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var cfg struct{ Milvus mc.ClientConfig }
	if err = json.Unmarshal(raw, &cfg); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	store, err := NewSkillMilvus(ctx, &cfg.Milvus)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Client.Close(ctx)
	name := "skill_fixture_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	if err = store.CreateDataset(ctx, name, "", 3, ""); err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := store.DropTable(context.Background(), name); err != nil {
			t.Error(err)
		}
	}()
	old := map[string]interface{}{"skill_id": "orange", "description": "published-old", "_search_text": "orange instructions", "q_3_vec": []float64{1, 2, 3}}
	if err = store.IndexDocument(ctx, name, "orange", old); err != nil {
		t.Fatal(err)
	}
	physical, err := store.resolve(ctx, name)
	if err != nil {
		t.Fatal(err)
	}
	newer := map[string]interface{}{"skill_id": "orange", "description": "unpublished-new", "_search_text": strings.Repeat("orange ", 15000), "q_3_vec": []float64{1, 2, 3}}
	rows, head, err := skillDocumentFragments("orange", newer)
	if err != nil {
		t.Fatal(err)
	}
	injected := errors.New("injected pre-publication failure")
	err = publishSkillDocument(rows, head, func(batch []skillFragment) error {
		if err := store.writeFragments(ctx, physical, batch); err != nil {
			return err
		}
		return injected
	})
	if !errors.Is(err, injected) {
		t.Fatalf("failure injection: %v", err)
	}
	result, err := store.Search(ctx, &types.SearchRequest{IndexNames: []string{name}, Limit: 1, MatchExprs: []interface{}{&types.MatchTextExpr{MatchingText: "orange"}}})
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Chunks) != 1 || result.Chunks[0]["description"] != "published-old" {
		t.Fatalf("unpublished fragments displaced published document: %#v", result)
	}
	if err = store.DeleteDocument(ctx, name, "orange"); err != nil {
		t.Fatal(err)
	}
	read, err := store.Client.Query(ctx, mc.NewQueryOption(physical).WithFilter("doc_id == \"orange\"").WithOutputFields("count(*)").WithConsistencyLevel(me.ClStrong))
	if err != nil {
		t.Fatal(err)
	}
	unicodeID := strings.Repeat("中", 255)
	old["skill_id"] = unicodeID
	if err = store.IndexDocument(ctx, name, unicodeID, old); err != nil {
		t.Fatal(err)
	}
	if err = store.DeleteDocument(ctx, name, unicodeID); err != nil {
		t.Fatal(err)
	}
	count, err := read.GetColumn("count(*)").Get(0)
	if err != nil || count != int64(0) {
		t.Fatalf("fragments remain: %v %v", count, err)
	}
}
