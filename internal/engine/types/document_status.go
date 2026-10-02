package types

import "context"

// DocumentStatusStore owns one connection for count, write and recovery.
type DocumentStatusStore interface {
	DocumentChunkCount(context.Context, string, string, string) (int64, error)
	UpdateDataset(context.Context, map[string]interface{}, map[string]interface{}, string, string) error
	Close() error
}
