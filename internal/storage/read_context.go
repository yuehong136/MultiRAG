package storage

import (
	"context"
	"fmt"
	"io"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/minio/minio-go/v7"
)

// ContextReader reads the exact registered object with cancellation and a bound
// on the entire body read. Implementations must close bodies before returning.
type ContextReader interface {
	GetContext(context.Context, string, string, ...string) ([]byte, error)
}

func (m *MinioStorage) GetContext(ctx context.Context, bucket, key string, _ ...string) ([]byte, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if m == nil || m.client == nil {
		return nil, fmt.Errorf("storage is not initialized")
	}
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	bucket, key = m.resolveBucketAndPath(bucket, key)
	object, err := m.client.GetObject(ctx, bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return nil, readMinioError(err)
	}
	defer object.Close()
	data, err := io.ReadAll(object)
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	if err != nil {
		return nil, readMinioError(err)
	}
	return data, nil
}

func readMinioError(err error) error {
	switch minio.ToErrorResponse(err).Code {
	case "NoSuchKey", "NoSuchObject", "NoSuchBucket", "NotFound":
		return ErrNotFound
	}
	return err
}

func readS3Context(ctx context.Context, client *s3.Client, bucket, key string) ([]byte, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if client == nil {
		return nil, fmt.Errorf("storage is not initialized")
	}
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	object, err := client.GetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(bucket), Key: aws.String(key)})
	if err != nil {
		if isObjectNotFound(err) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	defer object.Body.Close()
	data, err := io.ReadAll(object.Body)
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	return data, err
}

func (s *S3Storage) GetContext(ctx context.Context, bucket, key string, _ ...string) ([]byte, error) {
	if s == nil {
		return nil, fmt.Errorf("storage is not initialized")
	}
	effective := s
	if s.config != nil {
		copy := *s
		copy.bucket = s.config.Bucket
		copy.prefixPath = s.config.PrefixPath
		effective = &copy
	}
	bucket, key = effective.resolveBucketAndPath(bucket, key)
	return readS3Context(ctx, s.client, bucket, key)
}
func (o *OSSStorage) GetContext(ctx context.Context, bucket, key string, _ ...string) ([]byte, error) {
	if o == nil {
		return nil, fmt.Errorf("storage is not initialized")
	}
	bucket, key = o.resolveBucketAndPath(bucket, key)
	return readS3Context(ctx, o.client, bucket, key)
}
