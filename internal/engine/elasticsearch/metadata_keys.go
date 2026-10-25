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

// DeleteMetadataKeys preserves Python physical IDs and updates each row atomically.
func (e *elasticsearchEngine) DeleteMetadataKeys(ctx context.Context, id, kb string, keys []string, tenant string) error {
	index, err := metadataIndex(tenant)
	if err != nil {
		return err
	}
	if strings.TrimSpace(id) == "" || strings.TrimSpace(kb) == "" {
		return fmt.Errorf("metadata document and dataset are required")
	}
	if err := types.ValidateMetadataKeys(keys); err != nil {
		return err
	}
	identities, err := e.metadataIdentities(ctx, index, []map[string]interface{}{{"id": id, "kb_id": kb}})
	if err != nil {
		return err
	}
	physicalID := identities[storedID(kb, id)]
	if physicalID == "" {
		return fmt.Errorf("metadata document not found")
	}
	body := map[string]interface{}{"script": map[string]interface{}{
		"lang":   "painless",
		"source": `if (ctx._source.id != params.id || ctx._source.kb_id != params.kb) { throw new IllegalArgumentException('metadata scope mismatch'); } if (!(ctx._source.meta_fields instanceof Map)) { throw new IllegalArgumentException('invalid metadata object'); } boolean changed=false; for (def key : params.keys) { if (ctx._source.meta_fields.containsKey(key)) { ctx._source.meta_fields.remove(key); changed=true; } } if (!changed) { ctx.op='noop'; } else if (ctx._source.meta_fields.isEmpty()) { ctx.op='delete'; }`,
		"params": map[string]interface{}{"id": id, "kb": kb, "keys": keys},
	}}
	raw, err := json.Marshal(body)
	if err != nil {
		return err
	}
	retries := 3
	res, err := (&esapi.UpdateRequest{Index: index, DocumentID: physicalID, Body: bytes.NewReader(raw), Refresh: "wait_for", RetryOnConflict: &retries}).Do(ctx, e.client)
	if err != nil {
		return err
	}
	var result struct {
		Result string `json:"result"`
	}
	if err := responseJSON(res, &result); err != nil {
		return err
	}
	switch result.Result {
	case "updated", "deleted", "noop":
		return nil
	}
	return fmt.Errorf("metadata key deletion was not acknowledged")
}
