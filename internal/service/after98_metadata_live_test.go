package service

import (
	"context"
	"encoding/json"
	"multirag/internal/dao"
	"multirag/internal/engine/elasticsearch"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/testsupport/scratch"
	"reflect"
	"testing"
	"time"
)

func TestAfter98MetadataSQLAndElasticsearch(t *testing.T) {
	f := scratch.New(t)
	old := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = old })
	if err := f.DB.AutoMigrate(&entity.Knowledgebase{}); err != nil {
		t.Fatal(err)
	}
	tenant := f.Prefix
	kb := f.Prefix + "kb"
	doc := f.Prefix + "doc"
	if err := f.DB.Create(&entity.Knowledgebase{ID: kb, TenantID: tenant, Name: "Metadata", CreatedBy: tenant, EmbdID: "m", Status: func() *string { value := "1"; return &value }()}).Error; err != nil {
		t.Fatal(err)
	}
	indexName := BuildMetadataIndexName(tenant)
	f.Journal.Event("reserve-metadata-index", map[string]any{"index": indexName})
	index, err := elasticsearch.NewEngine(&f.Config.Elasticsearch)
	if err != nil {
		t.Fatal("connect owned Elasticsearch")
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := index.DropMetadataStore(ctx, tenant); err != nil {
			t.Error("drop owned metadata index")
		}
		reader, err := elasticsearch.NewEngine(&f.Config.Elasticsearch)
		if err != nil {
			t.Error("independent ES cleanup client")
			return
		}
		exists, err := reader.MetadataStoreExists(ctx, tenant)
		f.Journal.Event("cleanup-metadata-index-readback", map[string]any{"index": indexName, "absent": err == nil && !exists})
		if err != nil || exists {
			t.Error("owned metadata index survived")
		}
	})
	if err = index.CreateMetadataStore(t.Context(), tenant); err != nil {
		t.Fatal(err)
	}
	values := map[string]interface{}{"large": json.Number("9007199254740993"), "zero": 0, "flag": false, "empty": "", "version": "v2"}
	// IndexDocument refreshes synchronously. This is the same metadata index and
	// row representation read by the canonical SearchMetadata primitive.
	if err = index.IndexDocument(t.Context(), indexName, doc, map[string]interface{}{"id": doc, "kb_id": kb, "meta_fields": values}); err != nil {
		t.Fatal(err)
	}
	reader, err := elasticsearch.NewEngine(&f.Config.Elasticsearch)
	if err != nil {
		t.Fatal("independent metadata readback client")
	}
	result, err := reader.SearchMetadata(t.Context(), &types.SearchMetadataRequest{TenantID: tenant, Limit: 10, Filter: map[string]interface{}{"id": []string{doc}, "kb_id": kb}})
	if err != nil || len(result.MetadataRecords) != 1 {
		t.Fatal("persisted metadata readback", err)
	}
	decoded, err := ExtractMetaFields(result.MetadataRecords[0])
	if err != nil || decoded["large"] != json.Number("9007199254740993") || decoded["flag"] != false {
		t.Fatal("exact metadata SQL/ES boundary lost scalar", decoded, err)
	}
	s := &MetadataService{docEngine: reader}
	flat, err := s.GetFlattedMetaByKBs([]string{kb})
	if err != nil || !reflect.DeepEqual(flat["large"]["9007199254740993"], []string{doc}) || !reflect.DeepEqual(flat["zero"]["0"], []string{doc}) {
		t.Fatal(flat, err)
	}
	ids, empty, err := ApplyMetaDataFilter(t.Context(), manualMetadataConfig([]MetaFilterCondition{{Key: "version", Op: "is", Value: "v2"}}, "and"), flat, "q", nil, nil, nil)
	if err != nil || empty || !reflect.DeepEqual(ids, []string{doc}) {
		t.Fatal(ids, empty, err)
	}
	f.Journal.Event("canonical-metadata-readback", map[string]any{"documents": 1, "exact_integer_preserved": true, "false_zero_preserved": true})
}
