package elasticsearch

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"github.com/elastic/go-elasticsearch/v8/esapi"
	"strings"
)

func (e *elasticsearchEngine) CreateMetadataStore(ctx context.Context, tenant string) error {
	index, err := metadataIndex(tenant)
	if err != nil {
		return err
	}
	exists, err := e.indexExists(ctx, index)
	if err != nil || exists {
		return err
	}
	return e.createIndex(ctx, index, "doc_meta_es_mapping.json")
}
func (e *elasticsearchEngine) MetadataStoreExists(ctx context.Context, tenant string) (bool, error) {
	index, err := metadataIndex(tenant)
	if err != nil {
		return false, err
	}
	return e.indexExists(ctx, index)
}
func (e *elasticsearchEngine) DropMetadataStore(ctx context.Context, tenant string) error {
	index, err := metadataIndex(tenant)
	if err != nil {
		return err
	}
	return e.dropIndex(ctx, index)
}
func (e *elasticsearchEngine) InsertMetadata(ctx context.Context, documents []map[string]interface{}, tenant string) ([]string, error) {
	index, err := metadataIndex(tenant)
	if err != nil {
		return nil, err
	}
	// Validate the entire batch before creating a store or performing any writes.
	seen := map[string]bool{}
	for _, doc := range documents {
		key := fmt.Sprint(doc["kb_id"]) + "\x00" + fmt.Sprint(doc["id"])
		if seen[key] {
			return nil, fmt.Errorf("duplicate metadata id")
		}
		seen[key] = true
		if _, err := metadataFields(doc["meta_fields"]); err != nil {
			return nil, err
		}
		if id, ok := doc["id"].(string); !ok || strings.TrimSpace(id) == "" {
			return nil, fmt.Errorf("metadata id is required")
		}
		if kb, ok := doc["kb_id"].(string); !ok || strings.TrimSpace(kb) == "" {
			return nil, fmt.Errorf("metadata kb_id is required")
		}
	}
	if len(documents) == 0 {
		return []string{}, nil
	}
	exists, err := e.indexExists(ctx, index)
	if err != nil {
		return nil, err
	}
	if !exists {
		if err = e.CreateMetadataStore(ctx, tenant); err != nil {
			return nil, err
		}
	}
	identities, err := e.metadataIdentities(ctx, index, documents)
	if err != nil {
		return nil, err
	}
	return e.bulkInsert(ctx, index, documents, "", identities)
}
func metadataFields(v interface{}) (map[string]interface{}, error) {
	fields, ok := v.(map[string]interface{})
	if !ok || fields == nil {
		return nil, fmt.Errorf("metadata must be an object")
	}
	if _, err := json.Marshal(fields); err != nil {
		return nil, err
	}
	return fields, nil
}
func (e *elasticsearchEngine) UpdateMetadata(ctx context.Context, id, kb string, fields map[string]interface{}, tenant string) error {
	index, err := metadataIndex(tenant)
	if err != nil {
		return err
	}
	if strings.TrimSpace(id) == "" || strings.TrimSpace(kb) == "" {
		return fmt.Errorf("metadata document and dataset are required")
	}
	if _, err = metadataFields(fields); err != nil {
		return err
	}
	identities, err := e.metadataIdentities(ctx, index, []map[string]interface{}{{"id": id, "kb_id": kb}})
	if err != nil {
		return err
	}
	physicalID := storedID(kb, id)
	if existing, ok := identities[physicalID]; ok {
		physicalID = existing
	}
	body := map[string]interface{}{"scripted_upsert": true, "upsert": map[string]interface{}{"id": id, "kb_id": kb, "meta_fields": map[string]interface{}{}}, "script": map[string]interface{}{"lang": "painless", "source": `if (ctx._source.id != params.id || ctx._source.kb_id != params.kb) { throw new IllegalArgumentException('metadata scope mismatch'); } if (ctx._source.meta_fields == null) { ctx._source.meta_fields = new HashMap(); } ctx._source.meta_fields.putAll(params.fields);`, "params": map[string]interface{}{"id": id, "kb": kb, "fields": fields}}}
	raw, err := json.Marshal(body)
	if err != nil {
		return err
	}
	res, err := (&esapi.UpdateRequest{Index: index, DocumentID: physicalID, Body: bytes.NewReader(raw), Refresh: "wait_for", RetryOnConflict: func() *int { v := 3; return &v }()}).Do(ctx, e.client)
	if err != nil {
		return err
	}
	var result struct {
		Result string `json:"result"`
	}
	if err = responseJSON(res, &result); err != nil {
		return err
	}
	if result.Result != "updated" && result.Result != "created" && result.Result != "noop" {
		return fmt.Errorf("metadata update was not acknowledged")
	}
	return nil
}
func (e *elasticsearchEngine) DeleteMetadata(ctx context.Context, condition map[string]interface{}, tenant string) (int64, error) {
	index, err := metadataIndex(tenant)
	if err != nil {
		return 0, err
	}
	if len(condition) == 0 {
		return 0, fmt.Errorf("metadata deletion requires a condition")
	}
	query, err := conditionQuery(condition)
	if err != nil {
		return 0, err
	}
	exists, err := e.indexExists(ctx, index)
	if err != nil || !exists {
		return 0, err
	}
	return e.byQuery(ctx, index, query, nil, false)
}

// Preserve physical IDs already written by the Python metadata adapter while
// assigning stable, KB-scoped IDs to new rows. Duplicate logical rows fail.
func (e *elasticsearchEngine) metadataIdentities(ctx context.Context, index string, documents []map[string]interface{}) (map[string]string, error) {
	identities := map[string]string{}
	for start := 0; start < len(documents); start += 100 {
		end := min(start+100, len(documents))
		queries := make([]interface{}, 0, end-start)
		expected := map[string]bool{}
		for _, doc := range documents[start:end] {
			id, kb := doc["id"].(string), doc["kb_id"].(string)
			key := storedID(kb, id)
			expected[key] = true
			query, err := scopeQuery(map[string]interface{}{"id": id}, kb)
			if err != nil {
				return nil, err
			}
			queries = append(queries, query)
		}
		raw, err := json.Marshal(map[string]interface{}{"query": map[string]interface{}{"bool": map[string]interface{}{"should": queries, "minimum_should_match": 1}}, "_source": []string{"id", "kb_id"}, "size": 2 * (end - start), "track_total_hits": true})
		if err != nil {
			return nil, err
		}
		res, err := (&esapi.SearchRequest{Index: []string{index}, Body: bytes.NewReader(raw)}).Do(ctx, e.client)
		if err != nil {
			return nil, err
		}
		var result struct {
			Hits *struct {
				Total struct {
					Value int `json:"value"`
				} `json:"total"`
				Hits []struct {
					ID     string                 `json:"_id"`
					Source map[string]interface{} `json:"_source"`
				} `json:"hits"`
			} `json:"hits"`
		}
		if err = responseJSON(res, &result); err != nil {
			return nil, err
		}
		if result.Hits == nil || result.Hits.Total.Value != len(result.Hits.Hits) {
			return nil, fmt.Errorf("incomplete metadata identity query")
		}
		for _, hit := range result.Hits.Hits {
			id, ok := hit.Source["id"].(string)
			if !ok || id == "" {
				return nil, fmt.Errorf("invalid metadata document identity")
			}
			kb, ok := hit.Source["kb_id"].(string)
			if !ok || kb == "" || hit.ID == "" {
				return nil, fmt.Errorf("invalid metadata dataset identity")
			}
			key := storedID(kb, id)
			if !expected[key] || identities[key] != "" {
				return nil, fmt.Errorf("duplicate or foreign metadata row")
			}
			identities[key] = hit.ID
		}
	}
	return identities, nil
}
