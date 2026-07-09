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

	"github.com/milvus-io/milvus/client/v2/milvusclient"
)

// CreateDataset creates a collection in Milvus
func (e *milvusEngine) CreateDataset(ctx context.Context, indexName, datasetID string, vectorSize int, parserID string) error {
	// TODO: Implement collection creation with schema (vectorSize/parserID aware)
	return fmt.Errorf("milvus CreateDataset not yet implemented")
}

// CreateMetadata creates the document metadata collection in Milvus
func (e *milvusEngine) CreateMetadata(ctx context.Context, indexName string) error {
	// TODO: implement doc meta index for Milvus
	return nil
}

// InsertDataset inserts documents into a dataset collection in Milvus
func (e *milvusEngine) InsertDataset(ctx context.Context, documents []map[string]interface{}, indexName string, knowledgebaseID string) ([]string, error) {
	// TODO: implement dataset insert for Milvus
	return []string{}, nil
}

// InsertMetadata inserts documents into tenant's metadata collection in Milvus
func (e *milvusEngine) InsertMetadata(ctx context.Context, documents []map[string]interface{}, tenantID string) ([]string, error) {
	// TODO: implement metadata insert for Milvus
	return []string{}, nil
}

// UpdateDataset updates chunks by condition in Milvus
func (e *milvusEngine) UpdateDataset(ctx context.Context, condition map[string]interface{}, newValue map[string]interface{}, tableNamePrefix string, knowledgebaseID string) error {
	// TODO: implement dataset update for Milvus
	return nil
}

// UpdateMetadata updates document metadata in tenant's metadata collection in Milvus
func (e *milvusEngine) UpdateMetadata(ctx context.Context, docID string, kbID string, metaFields map[string]interface{}, tenantID string) error {
	// TODO: implement metadata update for Milvus
	return nil
}

// DropTable drops a collection in Milvus
func (e *milvusEngine) DropTable(ctx context.Context, indexName string) error {
	return e.client.DropCollection(ctx, milvusclient.NewDropCollectionOption(indexName))
}

// TableExists checks if a collection exists in Milvus
func (e *milvusEngine) TableExists(ctx context.Context, indexName string) (bool, error) {
	has, err := e.client.HasCollection(ctx, milvusclient.NewHasCollectionOption(indexName))
	if err != nil {
		return false, err
	}
	return has, nil
}

// Delete deletes rows from either a dataset collection or metadata collection in Milvus
func (e *milvusEngine) Delete(ctx context.Context, condition map[string]interface{}, indexName string, datasetID string) (int64, error) {
	// TODO: implement delete for Milvus
	return 0, nil
}
