package elasticsearch

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"github.com/elastic/go-elasticsearch/v8/esapi"
	"multirag/internal/engine/types"
	"strings"
)

func (e *elasticsearchEngine) CreateChunkStore(ctx context.Context, base, dataset string, vectorSize int, parserID string) error {
	if strings.TrimSpace(dataset) == "" || vectorSize <= 0 {
		return fmt.Errorf("dataset scope and positive vector dimension are required")
	}
	exists, err := e.indexExists(ctx, base)
	if err != nil {
		return err
	}
	if exists {
		body, err := json.Marshal(map[string]interface{}{"properties": map[string]interface{}{fmt.Sprintf("q_%d_vec", vectorSize): map[string]interface{}{"type": "dense_vector", "dims": vectorSize, "index": true, "similarity": "cosine"}}})
		if err != nil {
			return err
		}
		res, err := (&esapi.IndicesPutMappingRequest{Index: []string{base}, Body: bytes.NewReader(body)}).Do(ctx, e.client)
		if err != nil {
			return err
		}
		var result struct {
			Acknowledged bool `json:"acknowledged"`
		}
		if err = responseJSON(res, &result); err != nil {
			return err
		}
		if !result.Acknowledged {
			return fmt.Errorf("vector mapping update not acknowledged")
		}
		return nil
	}
	return e.createIndex(ctx, base, "es_mapping.json", vectorSize)
}
func (e *elasticsearchEngine) ChunkStoreExists(ctx context.Context, base, dataset string) (bool, error) {
	if strings.TrimSpace(dataset) == "" {
		return false, fmt.Errorf("dataset scope is required")
	}
	return e.indexExists(ctx, base)
}

// DropChunkStore removes only this dataset from the shared tenant index.
func (e *elasticsearchEngine) DropChunkStore(ctx context.Context, base, dataset string) error {
	_, err := e.DeleteChunks(ctx, map[string]interface{}{}, base, dataset)
	return err
}
func (e *elasticsearchEngine) InsertChunks(ctx context.Context, documents []map[string]interface{}, base, dataset string) ([]string, error) {
	if strings.TrimSpace(dataset) == "" {
		return nil, fmt.Errorf("dataset scope is required")
	}
	return e.bulkInsert(ctx, base, documents, dataset)
}
func (e *elasticsearchEngine) UpdateChunks(ctx context.Context, condition, newValue map[string]interface{}, base, dataset string) error {
	query, err := scopeQuery(condition, dataset)
	if err != nil {
		return err
	}
	if len(newValue) == 0 {
		return fmt.Errorf("empty chunk update")
	}
	fields := map[string]interface{}{}
	remove := map[string]interface{}{}
	for k, v := range newValue {
		if k == "remove" {
			var ok bool
			remove, ok = v.(map[string]interface{})
			if !ok {
				return fmt.Errorf("invalid remove operation")
			}
			for field, value := range remove {
				if field == "id" || field == "kb_id" || !types.ValidFieldName(field) {
					return fmt.Errorf("invalid remove field")
				}
				if _, _, err = types.ConditionValues(value); err != nil {
					return err
				}
			}
			continue
		}
		if k == "id" || k == "kb_id" || !types.ValidFieldName(k) {
			return fmt.Errorf("invalid chunk update field %q", k)
		}
		fields[k] = v
	}
	if len(fields) == 0 && len(remove) == 0 {
		return fmt.Errorf("empty chunk update")
	}
	script := map[string]interface{}{"lang": "painless", "source": `for (def entry : params.remove.entrySet()) { def key=entry.getKey(); if (ctx._source[key] instanceof List) { def value=entry.getValue(); ctx._source[key].removeIf(item -> item == value); } else if (ctx._source[key] != null) { def parts=ctx._source[key].splitOnToken('###'); def kept=new ArrayList(); for (def part : parts) { if (part != entry.getValue()) { kept.add(part); } } ctx._source[key]=String.join('###', kept); } } ctx._source.putAll(params.fields);`, "params": map[string]interface{}{"fields": fields, "remove": remove}}
	_, err = e.byQuery(ctx, base, query, script, true)
	return err
}
func (e *elasticsearchEngine) DeleteChunks(ctx context.Context, condition map[string]interface{}, base, dataset string) (int64, error) {
	query, err := scopeQuery(condition, dataset)
	if err != nil {
		return 0, err
	}
	exists, err := e.indexExists(ctx, base)
	if err != nil || !exists {
		return 0, err
	}
	return e.byQuery(ctx, base, query, nil, false)
}
