//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

package infinity

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	infinity "github.com/infiniflow/infinity-go-sdk"
	"go.uber.org/zap"

	"multirag/internal/common"
	"multirag/internal/utility"
)

// CreateMetadataStore creates the document metadata table
func (e *infinityEngine) CreateMetadataStore(ctx context.Context, tenantID string) error {
	if strings.TrimSpace(tenantID) == "" {
		return fmt.Errorf("tenant is required")
	}
	indexName := buildMetadataTableName(tenantID)
	// Get database
	db, err := e.client.conn.GetDatabase(e.client.dbName)
	if err != nil {
		return fmt.Errorf("Failed to get database: %w", err)
	}

	// Check if table already exists
	exists, err := e.tableExists(ctx, indexName)
	if err != nil {
		return fmt.Errorf("Failed to check if table exists: %w", err)
	}
	if exists {
		return fmt.Errorf("metadata table '%s' already exists", indexName)
	}

	// Use configured doc_meta mapping file
	fpMapping := filepath.Join(utility.GetProjectBaseDirectory(), "configs", e.docMetaMappingFileName)

	schemaData, err := os.ReadFile(fpMapping)
	if err != nil {
		return fmt.Errorf("Failed to read mapping file: %w", err)
	}

	var schema map[string]fieldInfo
	if err := json.Unmarshal(schemaData, &schema); err != nil {
		return fmt.Errorf("Failed to parse mapping file: %w", err)
	}

	// Build column definitions
	var columns infinity.TableSchema
	for fieldName, fieldInfo := range schema {
		col := infinity.ColumnDefinition{
			Name:     fieldName,
			DataType: fieldInfo.Type,
			Default:  fieldInfo.Default,
		}
		columns = append(columns, &col)
	}

	// Create table
	_, err = db.CreateTable(indexName, columns, infinity.ConflictTypeIgnore)
	if err != nil {
		return fmt.Errorf("Failed to create doc meta table: %w", err)
	}
	common.Debug("Infinity created doc meta table", zap.String("tableName", indexName))

	// Get table for creating indexes
	table, err := db.GetTable(indexName)
	if err != nil {
		return fmt.Errorf("Failed to get table: %w", err)
	}

	// Create secondary index on id
	_, err = table.CreateIndex(
		fmt.Sprintf("idx_%s_id", indexName),
		infinity.NewIndexInfo("id", infinity.IndexTypeSecondary, nil),
		infinity.ConflictTypeIgnore,
		"",
	)
	if err != nil {
		return fmt.Errorf("Failed to create secondary index on id: %w", err)
	}

	// Create secondary index on kb_id
	_, err = table.CreateIndex(
		fmt.Sprintf("idx_%s_kb_id", indexName),
		infinity.NewIndexInfo("kb_id", infinity.IndexTypeSecondary, nil),
		infinity.ConflictTypeIgnore,
		"",
	)
	if err != nil {
		return fmt.Errorf("Failed to create secondary index on kb_id: %w", err)
	}

	return nil
}

// InsertMetadata inserts document metadata into tenant's metadata table
// Table name format: multirag_doc_meta_{tenant_id}
// Auto-create the table if it doesn't exist
// Replace existing metadata with same id and kb_id
func (e *infinityEngine) InsertMetadata(ctx context.Context, metadata []map[string]interface{}, tenantID string) ([]string, error) {
	if strings.TrimSpace(tenantID) == "" {
		return nil, fmt.Errorf("tenant is required")
	}
	if len(metadata) == 0 {
		return []string{}, nil
	}
	seen := map[string]bool{}
	for _, m := range metadata {
		id, ok := m["id"].(string)
		if !ok || strings.TrimSpace(id) == "" {
			return nil, fmt.Errorf("metadata id is required")
		}
		kb, ok := m["kb_id"].(string)
		if !ok || strings.TrimSpace(kb) == "" {
			return nil, fmt.Errorf("metadata kb_id is required")
		}
		key := kb + "\x00" + id
		if seen[key] {
			return nil, fmt.Errorf("duplicate metadata id")
		}
		seen[key] = true
		if _, err := decodeMetadataFields(m["meta_fields"]); err != nil {
			return nil, err
		}
	}

	tableName := buildMetadataTableName(tenantID)
	common.Info("InfinityConnection.InsertMetadata called", zap.String("tableName", tableName), zap.Int("metaCount", len(metadata)))

	db, err := e.client.conn.GetDatabase(e.client.dbName)
	if err != nil {
		return nil, fmt.Errorf("Failed to get database: %w", err)
	}

	table, err := db.GetTable(tableName)
	if err != nil {
		// Table doesn't exist, try to create it
		if !isMissingTable(err) {
			return nil, fmt.Errorf("Failed to get table %s: %w", tableName, err)
		}

		// Create metadata table
		if createErr := e.CreateMetadataStore(ctx, tenantID); createErr != nil {
			return nil, fmt.Errorf("Failed to create metadata table: %w", createErr)
		}

		table, err = db.GetTable(tableName)
		if err != nil {
			return nil, fmt.Errorf("Failed to get table after creation: %w", err)
		}
	}

	// Transform metadata - convert meta_fields map to JSON string
	insertMetadata := make([]map[string]interface{}, len(metadata))
	for i, m := range metadata {
		d := make(map[string]interface{})
		for k, v := range m {
			if k == "meta_fields" {
				d["meta_fields"] = utility.ConvertMapToJSONString(v)
			} else {
				d[k] = v
			}
		}
		insertMetadata[i] = d
	}

	// Delete existing metadata with same id and kb_id, then insert new
	if len(insertMetadata) > 0 {
		idList := make([]string, len(insertMetadata))
		for i, m := range insertMetadata {
			// Escape single quotes in values to prevent SQL injection
			docID, literalErr := filterLiteral(m["id"].(string))
			if literalErr != nil {
				return nil, literalErr
			}
			kbID, literalErr := filterLiteral(m["kb_id"].(string))
			if literalErr != nil {
				return nil, literalErr
			}
			idList[i] = fmt.Sprintf("(id = %s AND kb_id = %s)", docID, kbID)
		}
		filter := strings.Join(idList, " OR ")
		common.Debug(fmt.Sprintf("Deleting existing metadata with filter: %s", filter))
		delResp, delErr := table.Delete(filter)
		if delErr != nil {
			return nil, fmt.Errorf("failed to replace existing metadata: %w", delErr)
		} else if delResp.DeletedRows > 0 {
			common.Info(fmt.Sprintf("Deleted %d existing metadata entries", delResp.DeletedRows))
		}
	}

	// Insert metadata
	_, err = table.Insert(insertMetadata)
	if err != nil {
		return nil, fmt.Errorf("Failed to insert metadata: %w", err)
	}

	common.Info("InfinityConnection.InsertMetadata result", zap.String("tableName", tableName), zap.Int("metaCount", len(metadata)))
	return []string{}, nil
}

// UpdateMetadata updates or inserts document metadata in tenant's metadata table.
// If a row with the given docID and kbID exists, it merges the new metadata with existing
// (new keys win). If no row exists, it inserts a new row.
// Table name format: multirag_doc_meta_{tenant_id}
func (e *infinityEngine) UpdateMetadata(ctx context.Context, docID string, kbID string, metaFields map[string]interface{}, tenantID string) error {
	if strings.TrimSpace(tenantID) == "" || strings.TrimSpace(docID) == "" || strings.TrimSpace(kbID) == "" {
		return fmt.Errorf("tenant, document and dataset are required")
	}
	if _, err := decodeMetadataFields(metaFields); err != nil {
		return err
	}
	tableName := buildMetadataTableName(tenantID)
	db, err := e.client.conn.GetDatabase(e.client.dbName)
	if err != nil {
		return err
	}
	table, err := db.GetTable(tableName)
	if err != nil {
		return err
	}
	filter, err := strictFilterFromCondition(map[string]interface{}{"id": docID, "kb_id": kbID}, nil)
	if err != nil {
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
	if count > 1 {
		return fmt.Errorf("duplicate metadata rows")
	}
	rows.Data["meta_fields"], err = decodeMetadataColumn(rows.Data["meta_fields"], count)
	if err != nil {
		return err
	}
	if count > 1 || len(rows.Data["kb_id"]) != count || len(rows.Data["meta_fields"]) != count {
		return fmt.Errorf("duplicate or malformed metadata rows")
	}
	merged := map[string]interface{}{}
	if count == 1 {
		if rows.Data["id"][0] != docID || rows.Data["kb_id"][0] != kbID {
			return fmt.Errorf("metadata scope mismatch")
		}
		merged, err = decodeMetadataFields(rows.Data["meta_fields"][0])
		if err != nil {
			return err
		}
	}
	for k, v := range metaFields {
		merged[k] = v
	}
	raw, err := json.Marshal(merged)
	if err != nil {
		return err
	}
	if count == 1 {
		_, err = table.Update(filter, map[string]interface{}{"meta_fields": string(raw)})
	} else {
		_, err = table.Insert(map[string]interface{}{"id": docID, "kb_id": kbID, "meta_fields": string(raw)})
	}
	return err
}
func decodeMetadataFields(value interface{}) (map[string]interface{}, error) {
	var fields map[string]interface{}
	switch v := value.(type) {
	case map[string]interface{}:
		fields = make(map[string]interface{}, len(v))
		for k, x := range v {
			fields[k] = x
		}
	case string:
		var err error
		fields, err = decodeMetadataJSON([]byte(v))
		if err != nil {
			return nil, fmt.Errorf("invalid stored metadata JSON: %w", err)
		}
	case []byte:
		var err error
		fields, err = decodeMetadataJSON(v)
		if err != nil {
			return nil, fmt.Errorf("invalid stored metadata JSON: %w", err)
		}
	default:
		return nil, fmt.Errorf("metadata must be an object")
	}
	if fields == nil {
		return nil, fmt.Errorf("metadata must be an object")
	}
	if _, err := json.Marshal(fields); err != nil {
		return nil, err
	}
	return fields, nil
}

func decodeMetadataJSON(raw []byte) (map[string]interface{}, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var fields map[string]interface{}
	if err := decoder.Decode(&fields); err != nil {
		return nil, err
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return nil, fmt.Errorf("metadata JSON contains trailing data")
	}
	return fields, nil
}

// The pinned SDK leaves JSON columns as one length-prefixed byte vector. Split
// it by row before consumers validate identities; never merge neighboring rows.
func decodeMetadataColumn(values []interface{}, count int) ([]interface{}, error) {
	if len(values) == 1 {
		if raw, ok := values[0].([]byte); ok {
			if json.Valid(raw) {
				if count != 1 {
					return nil, fmt.Errorf("incomplete metadata column")
				}
				object, err := decodeMetadataFields(raw)
				if err != nil {
					return nil, err
				}
				return []interface{}{object}, nil
			}
			decoded := make([]interface{}, 0, count)
			for len(raw) > 0 {
				if len(raw) < 4 {
					return nil, fmt.Errorf("truncated metadata length prefix")
				}
				length := uint64(binary.LittleEndian.Uint32(raw[:4]))
				raw = raw[4:]
				if length == 0 || length > uint64(len(raw)) {
					return nil, fmt.Errorf("invalid metadata length prefix")
				}
				object, err := decodeMetadataFields(raw[:int(length)])
				if err != nil {
					return nil, err
				}
				decoded = append(decoded, object)
				raw = raw[int(length):]
			}
			if len(decoded) != count {
				return nil, fmt.Errorf("metadata row count mismatch")
			}
			return decoded, nil
		}
	}
	if len(values) != count {
		return nil, fmt.Errorf("incomplete metadata column")
	}
	decoded := make([]interface{}, count)
	for i, value := range values {
		fields, err := decodeMetadataFields(value)
		if err != nil {
			return nil, err
		}
		decoded[i] = fields
	}
	return decoded, nil
}
