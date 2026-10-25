package storage

import (
	"context"
	"fmt"
	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
)

// Readiness probes the configured store without creating objects or buckets.
func Readiness(ctx context.Context) error {
	return readinessFor(ctx, GetStorageFactory().GetStorage())
}

func readinessFor(ctx context.Context, store Storage) error {
	switch s := store.(type) {
	case *MinioStorage:
		if s.client == nil {
			return fmt.Errorf("storage is not initialized")
		}
		if s.bucket != "" {
			exists, err := s.client.BucketExists(ctx, s.bucket)
			if err != nil {
				return err
			}
			if !exists {
				return fmt.Errorf("storage bucket unavailable")
			}
			return nil
		}
		_, err := s.client.ListBuckets(ctx)
		return err
	case *S3Storage:
		bucket := s.bucket
		if bucket == "" && s.config != nil {
			bucket = s.config.Bucket
		}
		if s.client == nil {
			return fmt.Errorf("storage is not initialized")
		}
		if bucket != "" {
			_, err := s.client.HeadBucket(ctx, &s3.HeadBucketInput{Bucket: aws.String(bucket)})
			return err
		}
		_, err := s.client.ListBuckets(ctx, &s3.ListBucketsInput{})
		return err
	case *OSSStorage:
		if s.client == nil {
			return fmt.Errorf("storage is not initialized")
		}
		if s.bucket != "" {
			_, err := s.client.HeadBucket(ctx, &s3.HeadBucketInput{Bucket: aws.String(s.bucket)})
			return err
		}
		_, err := s.client.ListBuckets(ctx, &s3.ListBucketsInput{})
		return err
	default:
		return fmt.Errorf("storage is not initialized or has no readiness probe")
	}
}
