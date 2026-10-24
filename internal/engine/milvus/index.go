package milvus

import (
	"context"
	"fmt"
	"github.com/milvus-io/milvus/client/v2/milvusclient"
)

// Generic dataset writes remain unavailable. Skills use their independent,
// implemented alias/replace/recovery contract in engine.SkillMilvus.
func (e *milvusEngine) CreateChunkStore(context.Context, string, string, int, string) error {
	return fmt.Errorf("milvus chunk stores are not implemented")
}
func (e *milvusEngine) InsertChunks(context.Context, []map[string]interface{}, string, string) ([]string, error) {
	return nil, fmt.Errorf("milvus chunk inserts are not implemented")
}
func (e *milvusEngine) UpdateChunks(context.Context, map[string]interface{}, map[string]interface{}, string, string) error {
	return fmt.Errorf("milvus chunk updates are not implemented")
}
func (e *milvusEngine) DeleteChunks(context.Context, map[string]interface{}, string, string) (int64, error) {
	return 0, fmt.Errorf("milvus chunk deletion is not implemented")
}
func (e *milvusEngine) DropChunkStore(context.Context, string, string) error {
	return fmt.Errorf("milvus chunk store deletion is not implemented")
}
func (e *milvusEngine) ChunkStoreExists(ctx context.Context, base, dataset string) (bool, error) {
	if base == "" || dataset == "" {
		return false, fmt.Errorf("base name and dataset are required")
	}
	return e.client.HasCollection(ctx, milvusclient.NewHasCollectionOption(base+"_"+dataset))
}
func (e *milvusEngine) CreateMetadataStore(context.Context, string) error {
	return fmt.Errorf("milvus metadata stores are not implemented")
}
func (e *milvusEngine) InsertMetadata(context.Context, []map[string]interface{}, string) ([]string, error) {
	return nil, fmt.Errorf("milvus metadata inserts are not implemented")
}
func (e *milvusEngine) UpdateMetadata(context.Context, string, string, map[string]interface{}, string) error {
	return fmt.Errorf("milvus metadata updates are not implemented")
}
func (e *milvusEngine) DeleteMetadata(context.Context, map[string]interface{}, string) (int64, error) {
	return 0, fmt.Errorf("milvus metadata deletion is not implemented")
}
func (e *milvusEngine) DropMetadataStore(context.Context, string) error {
	return fmt.Errorf("milvus metadata store deletion is not implemented")
}
func (e *milvusEngine) MetadataStoreExists(context.Context, string) (bool, error) {
	return false, fmt.Errorf("milvus metadata stores are not implemented")
}
