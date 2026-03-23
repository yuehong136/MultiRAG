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

// IndexDocument inserts a single document into a Milvus collection
func (e *milvusEngine) IndexDocument(ctx context.Context, indexName, docID string, doc interface{}) error {
	// TODO: Implement single document insert
	return fmt.Errorf("milvus IndexDocument not yet implemented")
}

// BulkIndex inserts documents in bulk into a Milvus collection
func (e *milvusEngine) BulkIndex(ctx context.Context, indexName string, docs []interface{}) (interface{}, error) {
	// TODO: Implement bulk insert
	return nil, fmt.Errorf("milvus BulkIndex not yet implemented")
}

// GetDocument retrieves a document by ID from a Milvus collection
func (e *milvusEngine) GetDocument(ctx context.Context, indexName, docID string) (interface{}, error) {
	// TODO: Implement document retrieval by ID
	return nil, fmt.Errorf("milvus GetDocument not yet implemented")
}

// DeleteDocument deletes a document by ID from a Milvus collection
func (e *milvusEngine) DeleteDocument(ctx context.Context, indexName, docID string) error {
	// TODO: Implement document deletion by ID
	return fmt.Errorf("milvus DeleteDocument not yet implemented")
}
