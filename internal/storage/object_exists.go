package storage

import (
	"context"
	"errors"
	"fmt"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/smithy-go"
	"github.com/minio/minio-go/v7"
)

// ObjectExistsContext distinguishes confirmed absence from an unavailable store.
// Callers that compensate writes must not use the lossy ObjExist boolean.
func (m *MinioStorage) ObjectExistsContext(ctx context.Context, bucket, key string) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, err
	}
	if m == nil || m.client == nil {
		return false, fmt.Errorf("storage is not initialized")
	}
	bucket, key = m.resolveBucketAndPath(bucket, key)
	_, err := m.client.StatObject(ctx, bucket, key, minio.StatObjectOptions{})
	if err != nil {
		response := minio.ToErrorResponse(err)
		switch response.Code {
		case "NoSuchKey", "NoSuchObject", "NoSuchBucket", "NotFound":
			return false, nil
		default:
			return false, err
		}
	}
	return true, nil
}

// ObjectExistsContext distinguishes confirmed absence from an unavailable store.
func (s *S3Storage) ObjectExistsContext(ctx context.Context, bucket, key string) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, err
	}
	if s == nil || s.client == nil {
		return false, fmt.Errorf("storage is not initialized")
	}
	bucket, key = s.resolveBucketAndPath(bucket, key)
	_, err := s.client.HeadObject(ctx, &s3.HeadObjectInput{Bucket: aws.String(bucket), Key: aws.String(key)})
	if err != nil {
		if isObjectNotFound(err) {
			return false, nil
		}
		return false, err
	}
	return true, nil
}

// ObjectExistsContext distinguishes confirmed absence from an unavailable store.
func (o *OSSStorage) ObjectExistsContext(ctx context.Context, bucket, key string) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, err
	}
	if o == nil || o.client == nil {
		return false, fmt.Errorf("storage is not initialized")
	}
	bucket, key = o.resolveBucketAndPath(bucket, key)
	_, err := o.client.HeadObject(ctx, &s3.HeadObjectInput{Bucket: aws.String(bucket), Key: aws.String(key)})
	if err != nil {
		if isObjectNotFound(err) {
			return false, nil
		}
		return false, err
	}
	return true, nil
}

func isObjectNotFound(err error) bool {
	var apiError smithy.APIError
	if !errors.As(err, &apiError) {
		return false
	}
	switch apiError.ErrorCode() {
	case "NoSuchKey", "NoSuchBucket", "NotFound", "404":
		return true
	default:
		return false
	}
}
