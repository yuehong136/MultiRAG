package storage

import (
	"context"
	"errors"
	"testing"
)

func TestRemoveContextCancelledBeforeClientIO(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	for name, remove := range map[string]func(context.Context, string, string, ...string) error{
		"minio": (&MinioStorage{}).RemoveContext,
		"s3":    (&S3Storage{}).RemoveContext,
		"oss":   (&OSSStorage{}).RemoveContext,
	} {
		t.Run(name, func(t *testing.T) {
			if err := remove(ctx, "owned-bucket", "owned-key"); !errors.Is(err, context.Canceled) {
				t.Fatalf("cancelled delete: %v", err)
			}
		})
	}
}
