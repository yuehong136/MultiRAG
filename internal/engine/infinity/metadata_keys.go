package infinity

import (
	"context"
	"encoding/json"
	"fmt"
	infinity "github.com/infiniflow/infinity-go-sdk"
	"multirag/internal/engine/types"
	"strings"
)

// DeleteMetadataKeys uses the same strict row decoder as metadata updates.
func (e *infinityEngine) DeleteMetadataKeys(ctx context.Context, id, kb string, keys []string, tenant string) error {
	if strings.TrimSpace(tenant) == "" || strings.TrimSpace(id) == "" || strings.TrimSpace(kb) == "" {
		return fmt.Errorf("tenant, document and dataset are required")
	}
	if err := types.ValidateMetadataKeys(keys); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	filter, err := strictFilterFromCondition(map[string]interface{}{"id": id, "kb_id": kb}, nil)
	if err != nil {
		return err
	}
	db, err := e.client.conn.GetDatabase(e.client.dbName)
	if err != nil {
		return err
	}
	table, err := db.GetTable(buildMetadataTableName(tenant))
	if err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	result, err := table.Output([]string{"id", "kb_id", "meta_fields"}).Filter(filter).Limit(2).ToResult()
	if err != nil {
		return fmt.Errorf("metadata read failed: %w", err)
	}
	rows, ok := result.(*infinity.QueryResult)
	if !ok || rows == nil {
		return fmt.Errorf("invalid metadata result")
	}
	count := len(rows.Data["id"])
	if count == 0 {
		return fmt.Errorf("metadata document not found")
	}
	if count != 1 || len(rows.Data["kb_id"]) != 1 || rows.Data["id"][0] != id || rows.Data["kb_id"][0] != kb {
		return fmt.Errorf("duplicate or foreign metadata rows")
	}
	fields, err := decodeMetadataColumn(rows.Data["meta_fields"], count)
	if err != nil {
		return err
	}
	current, err := decodeMetadataFields(fields[0])
	if err != nil {
		return err
	}
	changed := false
	for _, key := range keys {
		if _, ok := current[key]; ok {
			delete(current, key)
			changed = true
		}
	}
	if !changed {
		return nil
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if len(current) == 0 {
		result, err := table.Delete(filter)
		if err != nil {
			return err
		}
		if result == nil || result.DeletedRows != 1 {
			return fmt.Errorf("metadata deletion count mismatch")
		}
		return nil
	}
	raw, err := json.Marshal(current)
	if err != nil {
		return err
	}
	_, err = table.Update(filter, map[string]interface{}{"meta_fields": string(raw)})
	return err
}
