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

package engine

import (
	"context"

	"multirag/internal/engine/types"
)

// EngineType document engine type
type EngineType string

const (
	EngineElasticsearch EngineType = "elasticsearch"
	EngineInfinity      EngineType = "infinity"
	EngineMilvus        EngineType = "milvus"
)

// DocEngine document storage engine interface
type DocEngine interface {
	// Search
	Search(ctx context.Context, req *types.SearchRequest) (*types.SearchResult, error)

	// Chunk stores use a base name and an explicit dataset scope. ES shares the
	// tenant index; Infinity resolves its per-dataset physical table internally.
	CreateChunkStore(ctx context.Context, baseName, datasetID string, vectorSize int, parserID string) error
	InsertChunks(ctx context.Context, chunks []map[string]interface{}, baseName, datasetID string) ([]string, error)
	UpdateChunks(ctx context.Context, condition, newValue map[string]interface{}, baseName, datasetID string) error
	DeleteChunks(ctx context.Context, condition map[string]interface{}, baseName, datasetID string) (int64, error)
	GetChunk(ctx context.Context, baseName, chunkID string, datasetIDs []string) (interface{}, error)
	DropChunkStore(ctx context.Context, baseName, datasetID string) error
	ChunkStoreExists(ctx context.Context, baseName, datasetID string) (bool, error)

	// Metadata stores are named by tenant, never inferred from a chunk base name.
	CreateMetadataStore(ctx context.Context, tenantID string) error
	InsertMetadata(ctx context.Context, metadata []map[string]interface{}, tenantID string) ([]string, error)
	// UpdateMetadata merges keys into the document row; new keys win.
	UpdateMetadata(ctx context.Context, docID, datasetID string, metaFields map[string]interface{}, tenantID string) error
	DeleteMetadata(ctx context.Context, condition map[string]interface{}, tenantID string) (int64, error)
	// DeleteMetadataKeys removes literal keys; deleting the last key removes the row.
	DeleteMetadataKeys(ctx context.Context, docID, datasetID string, keys []string, tenantID string) error
	DropMetadataStore(ctx context.Context, tenantID string) error
	MetadataStoreExists(ctx context.Context, tenantID string) (bool, error)

	// Utility functions for search result processing
	GetFields(chunks []map[string]interface{}, fields []string) map[string]map[string]interface{}
	GetAggregation(chunks []map[string]interface{}, fieldName string) []map[string]interface{}
	GetHighlight(chunks []map[string]interface{}, keywords []string, fieldName string) map[string]string
	GetDocIDs(chunks []map[string]interface{}) []string

	// Health check
	Ping(ctx context.Context) error
	Close() error
}

// ManagementChunkReader explicitly opts into reading disabled chunks after SQL
// authorization. Ordinary GetChunk callers retain the engine's retrieval policy.
type ManagementChunkReader interface {
	GetChunkForManagement(ctx context.Context, baseName, chunkID string, datasetIDs []string) (interface{}, error)
}

// Type returns the engine type (helper method for runtime type checking)
// This is a workaround since we can't import elasticsearch or infinity packages directly
func Type(docEngine DocEngine) EngineType {
	// Type checking through interface methods is not straightforward
	// This is a placeholder that should be implemented differently
	// or rely on configuration to know the type
	return EngineType("unknown")
}
