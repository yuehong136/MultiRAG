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
)

// GetChunk gets a chunk by ID.
// TODO: Implement chunk retrieval by ID (the Milvus engine is currently stubbed,
// consistent with Search/GetDocument/DeleteDocument).
func (e *milvusEngine) GetChunk(ctx context.Context, indexName, chunkID string, kbIDs []string) (interface{}, error) {
	return nil, fmt.Errorf("milvus GetChunk not yet implemented")
}
