package storage

import (
	"context"
	"fmt"
)

// ForRuntimeUploads retains the logical owner bucket when a configured physical
// bucket would otherwise erase it. Ordinary file callers keep their mappings.
func ForRuntimeUploads(store Storage) Storage {
	switch concrete := store.(type) {
	case *S3Storage:
		if concrete != nil && concrete.bucket != "" && concrete.prefixPath == "" {
			return &runtimeUploadStorage{Storage: store}
		}
	case *OSSStorage:
		if concrete != nil && concrete.bucket != "" {
			return &runtimeUploadStorage{Storage: store}
		}
	}
	return store
}

// These five methods are the complete runtime upload lifecycle. Forwarding every
// one through the same key mapping keeps verification and compensation scoped.
type runtimeUploadStorage struct {
	Storage
}

func (s *runtimeUploadStorage) Put(bucket, key string, content []byte, tenantID ...string) error {
	return s.Storage.Put(bucket, bucket+"/"+key, content, tenantID...)
}

func (s *runtimeUploadStorage) Get(bucket, key string, tenantID ...string) ([]byte, error) {
	return s.Storage.Get(bucket, bucket+"/"+key, tenantID...)
}

func (s *runtimeUploadStorage) Remove(bucket, key string, tenantID ...string) error {
	return s.RemoveContext(context.Background(), bucket, key, tenantID...)
}

func (s *runtimeUploadStorage) RemoveContext(ctx context.Context, bucket, key string, tenantID ...string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if remover, ok := s.Storage.(interface {
		RemoveContext(context.Context, string, string, ...string) error
	}); ok {
		return remover.RemoveContext(ctx, bucket, bucket+"/"+key, tenantID...)
	}
	return s.Storage.Remove(bucket, bucket+"/"+key, tenantID...)
}

func (s *runtimeUploadStorage) ObjectExistsContext(ctx context.Context, bucket, key string) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, err
	}
	checker, ok := s.Storage.(interface {
		ObjectExistsContext(context.Context, string, string) (bool, error)
	})
	if !ok {
		return false, fmt.Errorf("runtime storage cannot confirm object absence")
	}
	return checker.ObjectExistsContext(ctx, bucket, bucket+"/"+key)
}
