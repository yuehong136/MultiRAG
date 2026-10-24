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
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	infinity "github.com/infiniflow/infinity-go-sdk"
	"go.uber.org/zap"

	"multirag/internal/common"
	"multirag/internal/engine/types"
	"multirag/internal/utility"
)

// CreateChunkStore creates a dataset table in Infinity
// indexName is the table name prefix (e.g., "multirag_<tenant_id>")
// The full table name is built as "{indexName}_{datasetID}"
func (e *infinityEngine) CreateChunkStore(ctx context.Context, indexName, datasetID string, vectorSize int, parserID string) error {
	if vectorSize <= 0 {
		return fmt.Errorf("positive vector dimension is required")
	}
	vecSize := vectorSize

	// Build full table name: {indexName}_{datasetID}
	if err := validateChunkScope(indexName, datasetID); err != nil {
		return err
	}
	tableName := buildChunkTableName(indexName, datasetID)

	// Use configured schema
	fpMapping := filepath.Join(utility.GetProjectBaseDirectory(), "configs", e.mappingFileName)

	schemaData, err := os.ReadFile(fpMapping)
	if err != nil {
		return fmt.Errorf("Failed to read mapping file: %w", err)
	}

	var schema orderedFields
	if err := json.Unmarshal(schemaData, &schema); err != nil {
		return fmt.Errorf("Failed to parse mapping file: %w", err)
	}

	// Get database
	db, err := e.client.conn.GetDatabase(e.client.dbName)
	if err != nil {
		return fmt.Errorf("Failed to get database: %w", err)
	}

	// Check if table already exists
	exists, err := e.tableExists(ctx, tableName)
	if err != nil {
		return fmt.Errorf("Failed to check if table exists: %w", err)
	}
	if exists {
		return fmt.Errorf("table '%s' already exists", tableName)
	}

	// Build column definitions (preserving JSON order)
	var columns infinity.TableSchema
	for _, fieldName := range schema.Keys {
		fieldInfo := schema.Fields[fieldName]
		col := infinity.ColumnDefinition{
			Name:     fieldName,
			DataType: fieldInfo.Type,
			Default:  fieldInfo.Default,
		}
		columns = append(columns, &col)
	}

	// Add vector column
	vectorColName := fmt.Sprintf("q_%d_vec", vecSize)
	columns = append(columns, &infinity.ColumnDefinition{
		Name:     vectorColName,
		DataType: fmt.Sprintf("vector,%d,float", vecSize),
	})

	// Add chunk_data column for table parser
	if parserID == "table" {
		columns = append(columns, &infinity.ColumnDefinition{
			Name:     "chunk_data",
			DataType: "json",
			Default:  "{}",
		})
	}

	// Create table
	table, err := db.CreateTable(tableName, columns, infinity.ConflictTypeIgnore)
	if err != nil {
		return fmt.Errorf("Failed to create table: %w", err)
	}
	common.Debug("Infinity created table", zap.String("tableName", tableName))

	// Create HNSW index on vector column
	_, err = table.CreateIndex(
		"q_vec_idx",
		infinity.NewIndexInfo(vectorColName, infinity.IndexTypeHnsw, map[string]string{
			"M":               "16",
			"ef_construction": "50",
			"metric":          "cosine",
			"encode":          "lvq",
		}),
		infinity.ConflictTypeIgnore,
		"",
	)
	if err != nil {
		return fmt.Errorf("Failed to create HNSW index: %w", err)
	}

	// Create full-text indexes for varchar fields with analyzers
	for _, fieldName := range schema.Keys {
		fieldInfo := schema.Fields[fieldName]
		if fieldInfo.Type != "varchar" || fieldInfo.Analyzer == nil {
			continue
		}

		analyzers := []string{}
		switch a := fieldInfo.Analyzer.(type) {
		case string:
			analyzers = []string{a}
		case []interface{}:
			for _, v := range a {
				if s, ok := v.(string); ok {
					analyzers = append(analyzers, s)
				}
			}
		}

		for _, analyzer := range analyzers {
			indexNameFt := fmt.Sprintf("ft_%s_%s",
				regexp.MustCompile(`[^a-zA-Z0-9]`).ReplaceAllString(fieldName, "_"),
				regexp.MustCompile(`[^a-zA-Z0-9]`).ReplaceAllString(analyzer, "_"),
			)
			_, err = table.CreateIndex(
				indexNameFt,
				infinity.NewIndexInfo(fieldName, infinity.IndexTypeFullText, map[string]string{"ANALYZER": analyzer}),
				infinity.ConflictTypeIgnore,
				"",
			)
			if err != nil {
				return fmt.Errorf("Failed to create fulltext index %s: %w", indexNameFt, err)
			}
		}
	}

	// Create secondary indexes for fields with index_type
	for _, fieldName := range schema.Keys {
		fieldInfo := schema.Fields[fieldName]
		if fieldInfo.IndexType == nil {
			continue
		}

		indexTypeStr := ""
		params := map[string]string{}

		switch it := fieldInfo.IndexType.(type) {
		case string:
			indexTypeStr = it
		case map[string]interface{}:
			if t, ok := it["type"].(string); ok {
				indexTypeStr = t
			}
			if card, ok := it["cardinality"].(string); ok {
				params["cardinality"] = card
			}
		}

		if indexTypeStr == "secondary" {
			indexNameSec := fmt.Sprintf("sec_%s", fieldName)
			_, err = table.CreateIndex(
				indexNameSec,
				infinity.NewIndexInfo(fieldName, infinity.IndexTypeSecondary, params),
				infinity.ConflictTypeIgnore,
				"",
			)
			if err != nil {
				return fmt.Errorf("Failed to create secondary index %s: %w", indexNameSec, err)
			}
		}
	}

	return nil
}

// InsertChunks inserts chunks into a dataset table
// Table name format: {tableNamePrefix}_{knowledgebaseID}
// Auto-create the table if it doesn't exist
// Transform chunks before insert (see transformChunkFields)
// Delete existing rows with matching IDs before insert
func (e *infinityEngine) InsertChunks(ctx context.Context, chunks []map[string]interface{}, tableNamePrefix string, knowledgebaseID string) ([]string, error) {
	if err := validateChunkScope(tableNamePrefix, knowledgebaseID); err != nil {
		return nil, err
	}
	if len(chunks) == 0 {
		return []string{}, nil
	}
	seen := map[string]bool{}
	for _, chunk := range chunks {
		id, ok := chunk["id"].(string)
		if !ok || strings.TrimSpace(id) == "" || seen[id] {
			return nil, fmt.Errorf("missing or duplicate chunk id")
		}
		seen[id] = true
		values, _, err := types.ConditionValues(chunk["kb_id"])
		if err != nil || len(values) != 1 || values[0] != knowledgebaseID {
			return nil, fmt.Errorf("chunk belongs to another dataset")
		}
		if _, err = json.Marshal(chunk); err != nil {
			return nil, err
		}
	}

	tableName := buildChunkTableName(tableNamePrefix, knowledgebaseID)
	common.Info("InfinityConnection.InsertChunks called", zap.String("tableName", tableName), zap.Int("chunkCount", len(chunks)))

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

		// Infer vector size from chunks
		vectorSize := 0
		vectorPattern := regexp.MustCompile(`q_(\d+)_vec`)
		for _, chunk := range chunks {
			for key := range chunk {
				matches := vectorPattern.FindStringSubmatch(key)
				if len(matches) >= 2 {
					vectorSize, _ = strconv.Atoi(matches[1])
					break
				}
			}
			if vectorSize > 0 {
				break
			}
		}
		if vectorSize == 0 {
			return nil, fmt.Errorf("cannot infer vector size from chunks")
		}

		// Determine parser_id from chunk structure
		parserID := ""
		if chunkData, ok := chunks[0]["chunk_data"].(map[string]interface{}); ok && chunkData != nil {
			parserID = "table"
		}

		// Create table
		if err := e.CreateChunkStore(ctx, tableNamePrefix, knowledgebaseID, vectorSize, parserID); err != nil {
			return nil, fmt.Errorf("Failed to create table: %w", err)
		}

		table, err = db.GetTable(tableName)
		if err != nil {
			return nil, fmt.Errorf("Failed to get table after creation: %w", err)
		}
	}

	// Get embedding columns and their sizes
	var embeddingCols [][2]interface{}
	colsResp, err := table.ShowColumns()
	if err != nil {
		return nil, fmt.Errorf("Failed to get columns: %w", err)
	}
	result, ok := colsResp.(*infinity.QueryResult)
	if !ok {
		return nil, fmt.Errorf("unexpected response type: %T", colsResp)
	}

	// ShowColumns returns a result set where Data contains arrays of column values
	re := regexp.MustCompile(`Embedding\([a-z]+,(\d+)\)`)
	if nameArr, ok := result.Data["name"]; ok {
		if typeArr, ok := result.Data["type"]; ok {
			for i := 0; i < len(nameArr); i++ {
				colName, _ := nameArr[i].(string)
				colType, _ := typeArr[i].(string)
				matches := re.FindStringSubmatch(colType)
				if len(matches) >= 2 {
					size, _ := strconv.Atoi(matches[1])
					embeddingCols = append(embeddingCols, [2]interface{}{colName, size})
				}
			}
		}
	}

	// Transform chunks using helper function
	insertChunks := make([]map[string]interface{}, len(chunks))
	for i, chunk := range chunks {
		insertChunks[i] = transformChunkFields(chunk, embeddingCols)
	}

	// Delete existing rows with matching IDs
	if len(insertChunks) > 0 {
		idList := make([]string, len(insertChunks))
		for i, chunk := range insertChunks {
			idList[i], err = filterLiteral(chunk["id"].(string))
			if err != nil {
				return nil, err
			}
		}
		filter := fmt.Sprintf("id IN (%s)", strings.Join(idList, ", "))
		common.Debug(fmt.Sprintf("Deleting existing rows with filter: %s", filter))
		delResp, delErr := table.Delete(filter)
		if delErr != nil {
			return nil, fmt.Errorf("failed to replace existing chunks: %w", delErr)
		} else {
			common.Info(fmt.Sprintf("Deleted %d existing rows", delResp.DeletedRows))
		}
	}

	// Insert chunks to dataset
	_, err = table.Insert(insertChunks)
	if err != nil {
		return nil, fmt.Errorf("Failed to insert chunks to dataset: %w", err)
	}

	common.Info("InfinityConnection.InsertChunks result", zap.String("tableName", tableName), zap.Int("count", len(insertChunks)))
	return []string{}, nil
}

// UpdateChunks updates chunks in a dataset table
// Table name format: {tableNamePrefix}_{knowledgebaseID}
func (e *infinityEngine) UpdateChunks(ctx context.Context, condition map[string]interface{}, newValue map[string]interface{}, tableNamePrefix string, knowledgebaseID string) error {
	return e.updateChunks(ctx, condition, newValue, tableNamePrefix, knowledgebaseID, true)
}
func (e *infinityEngine) updateChunks(ctx context.Context, condition map[string]interface{}, newValue map[string]interface{}, tableNamePrefix string, knowledgebaseID string, bindDataset bool) error {
	if err := validateChunkScope(tableNamePrefix, knowledgebaseID); err != nil {
		return err
	}
	copyValue := make(map[string]interface{}, len(newValue))
	for k, v := range newValue {
		if k == "id" || k == "kb_id" {
			return fmt.Errorf("chunk identity cannot be changed")
		}
		copyValue[k] = v
	}
	newValue = copyValue
	if len(newValue) == 0 {
		return fmt.Errorf("empty chunk update")
	}
	scoped := make(map[string]interface{}, len(condition)+1)
	for k, v := range condition {
		scoped[k] = v
	}
	if supplied, ok := scoped["kb_id"]; ok {
		values, _, err := types.ConditionValues(supplied)
		if err != nil {
			return err
		}
		for _, v := range values {
			if v != knowledgebaseID {
				return fmt.Errorf("conflicting dataset scope")
			}
		}
	}
	if bindDataset {
		scoped["kb_id"] = knowledgebaseID
	}
	condition = scoped

	tableName := buildChunkTableName(tableNamePrefix, knowledgebaseID)
	common.Info("InfinityConnection.UpdateChunks called", zap.String("tableName", tableName), zap.Any("condition", condition))

	db, err := e.client.conn.GetDatabase(e.client.dbName)
	if err != nil {
		return fmt.Errorf("Failed to get database: %w", err)
	}

	table, err := db.GetTable(tableName)
	if err != nil {
		return fmt.Errorf("Failed to get table %s: %w", tableName, err)
	}

	// Get table columns
	clmns := make(map[string]struct {
		Type    string
		Default interface{}
	})
	colsResp, err := table.ShowColumns()
	if err != nil {
		return fmt.Errorf("Failed to get columns: %w", err)
	}
	result, ok := colsResp.(*infinity.QueryResult)
	if ok {
		if nameArr, ok := result.Data["name"]; ok {
			if typeArr, ok := result.Data["type"]; ok {
				if defArr, ok := result.Data["default"]; ok {
					for i := 0; i < len(nameArr); i++ {
						colName, _ := nameArr[i].(string)
						colType, _ := typeArr[i].(string)
						var colDefault interface{}
						if i < len(defArr) {
							colDefault = defArr[i]
						}
						clmns[colName] = struct {
							Type    string
							Default interface{}
						}{colType, colDefault}
					}
				}
			}
		}
	}

	// Build filter string from condition
	filter, err := strictFilterFromCondition(condition, clmns)
	if err != nil {
		return err
	}
	if target, ok := newValue["available_int"].(int); ok && len(newValue) == 1 && condition["doc_id"] != nil {
		if _, hasParent := clmns["mom_id"]; hasParent {
			parent, readErr := documentAvailabilityParents(table, filter)
			if readErr != nil {
				return readErr
			}
			if parent == "" {
				_, err = table.Update(filter, map[string]interface{}{"available_int": target})
				return err
			}
			if _, err := table.Update("("+filter+") AND ("+parent+")", map[string]interface{}{"available_int": 0}); err != nil {
				return err
			}
			_, err = table.Update("("+filter+") AND NOT ("+parent+")", map[string]interface{}{"available_int": target})
		} else {
			_, err = table.Update(filter, map[string]interface{}{"available_int": target})
		}
		return err
	}

	// Process remove operation first
	removeValue := make(map[string]interface{})
	if removeData, ok := newValue["remove"].(map[string]interface{}); ok {
		removeValue = removeData
	}
	delete(newValue, "remove")

	// Transform new_value fields using helper function (no embeddings needed for update)
	transformed := transformChunkFields(newValue, nil)
	for k, v := range transformed {
		newValue[k] = v
	}

	// Remove original fields that were transformed (they're now in transformed with new names/types)
	// Also remove intermediate token fields that shouldn't be stored in Infinity
	// This must match Python's delete list in infinity_conn.py
	for _, key := range []string{"docnm_kwd", "title_tks", "title_sm_tks", "important_kwd", "important_tks",
		"content_with_weight", "content_ltks", "content_sm_ltks", "authors_tks", "authors_sm_tks",
		"question_kwd", "question_tks"} {
		delete(newValue, key)
	}

	for field := range newValue {
		if _, ok := clmns[field]; !ok {
			return fmt.Errorf("unknown update column %q", field)
		}
	}
	for field, value := range removeValue {
		if _, ok := clmns[field]; !ok || !types.ValidFieldName(field) {
			return fmt.Errorf("unknown remove column %q", field)
		}
		if _, ok := value.(string); !ok {
			return fmt.Errorf("remove value must be a string")
		}
	}
	// Handle remove operations if any
	if len(removeValue) > 0 {
		colToRemove := make([]string, 0, len(removeValue))
		for k := range removeValue {
			colToRemove = append(colToRemove, k)
		}
		colToRemove = append(colToRemove, "id")

		// Query rows to be updated
		queryResult, err := table.Output(colToRemove).Filter(filter).ToResult()
		if err != nil {
			return fmt.Errorf("failed to query rows for remove operation: %w", err)
		} else {
			qr, ok := queryResult.(*infinity.QueryResult)
			if ok && len(qr.Data) > 0 {
				// Get the id column and columns to remove
				idCol := qr.Data["id"]
				removeOpt := make(map[string]map[string][]string) // column -> value -> [ids]

				for colName, colData := range qr.Data {
					if colName == "id" {
						continue
					}
					removeVal := removeValue[colName]
					for i, id := range idCol {
						if i < len(colData) {
							existingVal := colData[i]
							if removeStr, ok := removeVal.(string); ok {
								// Split existing value by ### and remove the target value
								if existingStr, ok := existingVal.(string); ok {
									parts := strings.Split(existingStr, "###")
									var newParts []string
									for _, p := range parts {
										if p != removeStr {
											newParts = append(newParts, p)
										}
									}
									if len(newParts) != len(parts) {
										idStr, literalErr := filterLiteral(fmt.Sprint(id))
										if literalErr != nil {
											return literalErr
										}
										if removeOpt[colName] == nil {
											removeOpt[colName] = make(map[string][]string)
										}
										removeOpt[colName][strings.Join(newParts, "###")] = append(removeOpt[colName][strings.Join(newParts, "###")], idStr)
									}
								}
							}
						}
					}
				}

				// Execute remove updates
				for colName, valueToIDs := range removeOpt {
					for newVal, ids := range valueToIDs {
						idFilter := filter + " AND id IN (" + strings.Join(ids, ", ") + ")"
						common.Info(fmt.Sprintf("INFINITY remove update: table=%s, idFilter=%s, column=%s, newValue=%v", tableName, idFilter, colName, newVal))
						_, err := table.Update(idFilter, map[string]interface{}{colName: newVal})
						if err != nil {
							return fmt.Errorf("failed to remove value from column %s: %w", colName, err)
						}
					}
				}
			}
		}
	}

	// Execute the main update
	if len(newValue) == 0 {
		return nil
	}
	common.Info(fmt.Sprintf("INFINITY update: table=%s, filter=%s, newValue=%v", tableName, filter, newValue))
	_, err = table.Update(filter, newValue)
	if err != nil {
		return fmt.Errorf("Failed to update chunks: %w", err)
	}

	common.Info("InfinityConnection.UpdateChunks completes", zap.String("tableName", tableName))
	return nil
}

// transformChunkFields transforms chunk field names/values for insert/update.
// It handles field name conversions and value transformations:
// - docnm_kwd -> docnm
// - title_kwd/title_sm_tks -> docnm (if docnm_kwd not set)
// - important_kwd -> important_keywords (+ important_kwd_empty_count)
// - content_with_weight/content_ltks/content_sm_ltks -> content
// - authors_tks/authors_sm_tks -> authors
// - question_kwd -> questions (joined with \n), question_tks -> questions (if question_kwd not set)
// - kb_id: list -> str (first element)
// - position_int: list -> hex_joined string
// - page_num_int, top_int: list -> hex string
// - *_feas fields -> JSON string
// - keyword fields with list values -> ### joined string
// - chunk_data: dict -> JSON string
// - Missing embeddings filled with zeros if embeddingCols provided
func transformChunkFields(chunk map[string]interface{}, embeddingCols [][2]interface{}) map[string]interface{} {
	d := make(map[string]interface{})

	for k, v := range chunk {
		switch k {
		case "docnm_kwd":
			d["docnm"] = v
		case "title_kwd":
			if _, exists := chunk["docnm_kwd"]; !exists {
				d["docnm"] = utility.ConvertToString(v)
			}
		case "title_sm_tks":
			if _, exists := chunk["docnm_kwd"]; !exists {
				d["docnm"] = utility.ConvertToString(v)
			}
		case "important_kwd":
			if list, ok := v.([]interface{}); ok {
				emptyCount := 0
				tokens := make([]string, 0)
				for _, item := range list {
					if str, ok := item.(string); ok {
						if str == "" {
							emptyCount++
						} else {
							tokens = append(tokens, str)
						}
					}
				}
				d["important_keywords"] = strings.Join(tokens, ",")
				d["important_kwd_empty_count"] = emptyCount
			} else {
				d["important_keywords"] = utility.ConvertToString(v)
			}
		case "important_tks":
			if _, exists := chunk["important_kwd"]; !exists {
				d["important_keywords"] = v
			}
		case "content_with_weight":
			d["content"] = v
		case "content_ltks":
			if _, exists := chunk["content_with_weight"]; !exists {
				d["content"] = v
			}
		case "content_sm_ltks":
			if _, exists := chunk["content_with_weight"]; !exists {
				d["content"] = v
			}
		case "authors_tks":
			d["authors"] = v
		case "authors_sm_tks":
			if _, exists := chunk["authors_tks"]; !exists {
				d["authors"] = v
			}
		case "question_kwd":
			d["questions"] = strings.Join(utility.ConvertToStringSlice(v), "\n")
		case "tag_kwd":
			d["tag_kwd"] = strings.Join(utility.ConvertToStringSlice(v), "###")
		case "question_tks":
			if _, exists := chunk["question_kwd"]; !exists {
				d["questions"] = utility.ConvertToString(v)
			}
		case "kb_id":
			if list, ok := v.([]interface{}); ok && len(list) > 0 {
				d["kb_id"] = list[0]
			} else {
				d["kb_id"] = v
			}
		case "position_int":
			if list, ok := v.([]interface{}); ok {
				d["position_int"] = utility.ConvertPositionIntArrayToHex(list)
			} else {
				d["position_int"] = v
			}
		case "page_num_int", "top_int":
			if list, ok := v.([]interface{}); ok {
				d[k] = utility.ConvertIntArrayToHex(list)
			} else {
				d[k] = v
			}
		case "chunk_data":
			d["chunk_data"] = utility.ConvertMapToJSONString(v)
		default:
			// Check for *_feas fields
			if strings.HasSuffix(k, "_feas") {
				jsonBytes, _ := json.Marshal(v)
				d[k] = string(jsonBytes)
			} else if fieldKeyword(k) {
				// keyword fields with list values -> ### joined
				if list, ok := v.([]interface{}); ok {
					d[k] = strings.Join(utility.ConvertToStringSlice(list), "###")
				} else {
					d[k] = v
				}
			} else {
				d[k] = v
			}
		}
	}

	// Remove intermediate token fields
	for _, key := range []string{"docnm_kwd", "title_tks", "title_sm_tks", "important_kwd", "important_tks",
		"content_with_weight", "content_ltks", "content_sm_ltks", "authors_tks", "authors_sm_tks",
		"question_kwd", "question_tks"} {
		delete(d, key)
	}

	// Fill missing embedding columns with zeros (raw slice, matching Python SDK)
	for _, ec := range embeddingCols {
		name, ok1 := ec[0].(string)
		size, ok2 := ec[1].(int)
		if !ok1 || !ok2 {
			continue
		}
		if _, exists := d[name]; !exists {
			zeros := make([]float64, size)
			for i := range zeros {
				zeros[i] = 0
			}
			d[name] = zeros
		}
	}

	return d
}

// UpdateDataset is the independent DocumentStatusStore contract. Its private
// connection and the service's SQL lock/recovery continue to own status changes.
func (e *infinityEngine) UpdateDataset(ctx context.Context, condition, newValue map[string]interface{}, base, dataset string) error {
	return e.updateChunks(ctx, condition, newValue, base, dataset, false)
}
