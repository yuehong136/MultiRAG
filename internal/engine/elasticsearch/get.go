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

package elasticsearch

import (
	"context"
	"fmt"
	"strings"

	"multirag/internal/engine/types"
)

// GetChunk gets a chunk by ID from an Elasticsearch index.
func (e *elasticsearchEngine) GetChunk(ctx context.Context, indexName, chunkID string, kbIDs []string) (interface{}, error) {
	return e.getChunk(ctx, indexName, chunkID, kbIDs, false)
}

func (e *elasticsearchEngine) GetChunkForManagement(ctx context.Context, indexName, chunkID string, kbIDs []string) (interface{}, error) {
	return e.getChunk(ctx, indexName, chunkID, kbIDs, true)
}

func (e *elasticsearchEngine) getChunk(ctx context.Context, indexName, chunkID string, kbIDs []string, includeUnavailable bool) (interface{}, error) {
	if len(kbIDs) == 0 {
		return nil, nil
	}
	if strings.TrimSpace(indexName) == "" || strings.TrimSpace(chunkID) == "" {
		return nil, fmt.Errorf("index and chunk IDs are required")
	}
	seen := map[string]bool{}
	for _, id := range kbIDs {
		if strings.TrimSpace(id) == "" || seen[id] {
			return nil, fmt.Errorf("distinct non-empty dataset IDs are required")
		}
		seen[id] = true
	}
	searchReq := &types.SearchRequest{
		IndexNames:         []string{indexName},
		KbIDs:              kbIDs,
		Limit:              1,
		IncludeUnavailable: includeUnavailable,
		Filter: map[string]interface{}{
			"id": chunkID,
		},
	}

	searchResp, err := e.Search(ctx, searchReq)
	if err != nil {
		return nil, fmt.Errorf("failed to search: %w", err)
	}

	if len(searchResp.Chunks) == 0 {
		return nil, nil
	}

	return searchResp.Chunks[0], nil
}
