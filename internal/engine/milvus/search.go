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

package milvus

import (
	"context"
	"fmt"
	"strings"

	"multirag/internal/engine/types"
)

// Search executes a search query against a Milvus collection
func (e *milvusEngine) Search(ctx context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	// TODO: Implement hybrid search (vector + scalar filtering)
	return nil, fmt.Errorf("milvus Search not yet implemented")
}

func (e *milvusEngine) GetFields(chunks []map[string]interface{}, fields []string) map[string]map[string]interface{} {
	result := make(map[string]map[string]interface{}, len(chunks))
	for _, chunk := range chunks {
		id, _ := chunk["id"].(string)
		if id == "" {
			id, _ = chunk["_id"].(string)
		}
		if id == "" {
			continue
		}
		selected := chunk
		if len(fields) > 0 {
			selected = make(map[string]interface{}, len(fields))
			for _, field := range fields {
				if value, ok := chunk[field]; ok {
					selected[field] = value
				}
			}
		}
		result[id] = selected
	}
	return result
}

func (e *milvusEngine) GetAggregation(chunks []map[string]interface{}, fieldName string) []map[string]interface{} {
	counts := make(map[string]int)
	for _, chunk := range chunks {
		if value, ok := chunk[fieldName].(string); ok && value != "" {
			counts[value]++
		}
	}
	result := make([]map[string]interface{}, 0, len(counts))
	for value, count := range counts {
		result = append(result, map[string]interface{}{"value": value, "count": count})
	}
	return result
}

func (e *milvusEngine) GetHighlight(chunks []map[string]interface{}, keywords []string, fieldName string) map[string]string {
	result := make(map[string]string)
	for _, chunk := range chunks {
		id, _ := chunk["id"].(string)
		if id == "" {
			id, _ = chunk["_id"].(string)
		}
		text, _ := chunk[fieldName].(string)
		if id == "" || text == "" {
			continue
		}
		for _, keyword := range keywords {
			if keyword != "" {
				text = strings.ReplaceAll(text, keyword, "<em>"+keyword+"</em>")
			}
		}
		result[id] = text
	}
	return result
}

func (e *milvusEngine) GetDocIDs(chunks []map[string]interface{}) []string {
	result := make([]string, 0, len(chunks))
	for _, chunk := range chunks {
		id, _ := chunk["id"].(string)
		if id == "" {
			id, _ = chunk["_id"].(string)
		}
		if id != "" {
			result = append(result, id)
		}
	}
	return result
}
