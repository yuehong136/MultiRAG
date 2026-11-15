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

package storage

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"multirag/internal/server"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/smithy-go"
	"go.uber.org/zap"
)

// S3Storage implements Storage interface for AWS S3
type S3Storage struct {
	client     *s3.Client
	bucket     string
	prefixPath string
	config     *server.S3Config
}

// NewS3Storage creates a new S3 storage instance
func NewS3Storage(config *server.S3Config) (*S3Storage, error) {
	if config == nil {
		return nil, fmt.Errorf("s3 configuration is required")
	}
	configCopy := *config
	config = &configCopy

	storage := &S3Storage{
		bucket:     config.Bucket,
		prefixPath: config.PrefixPath,
		config:     config,
	}

	if err := storage.connect(); err != nil {
		return nil, err
	}

	return storage, nil
}

func (s *S3Storage) connect() error {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	var opts []func(*config.LoadOptions) error

	// Configure region
	if s.config.Region != "" {
		opts = append(opts, config.WithRegion(s.config.Region))
	}

	// Configure credentials if provided
	if s.config.AccessKey != "" && s.config.SecretKey != "" {
		creds := credentials.NewStaticCredentialsProvider(
			s.config.AccessKey,
			s.config.SecretKey,
			s.config.SessionToken,
		)
		opts = append(opts, config.WithCredentialsProvider(creds))
	}

	// Load configuration
	cfg, err := config.LoadDefaultConfig(ctx, opts...)
	if err != nil {
		return fmt.Errorf("failed to load AWS config: %w", err)
	}

	// Create S3 client with custom endpoint if provided
	clientOpts := []func(*s3.Options){}
	if s.config.EndpointURL != "" {
		clientOpts = append(clientOpts, func(o *s3.Options) {
			o.BaseEndpoint = aws.String(s.config.EndpointURL)
			o.UsePathStyle = s.config.AddressingStyle == "path"
		})
	}

	s.client = s3.NewFromConfig(cfg, clientOpts...)
	return nil
}

func (s *S3Storage) reconnect() {
	if err := s.connect(); err != nil {
		zap.L().Error("Failed to reconnect to S3", zap.Error(err))
	}
}

func (s *S3Storage) resolveBucketAndPath(bucket, fnm string) (string, string) {
	actualBucket := bucket
	if s.bucket != "" {
		actualBucket = s.bucket
	}
	actualPath := fnm
	if s.bucket != "" {
		actualPath = fmt.Sprintf("%s/%s", bucket, fnm)
	}
	if s.prefixPath != "" {
		actualPath = fmt.Sprintf("%s/%s", s.prefixPath, actualPath)
	}
	return actualBucket, actualPath
}

// Health checks S3 service availability
func (s *S3Storage) Health() bool {
	if s.client == nil {
		return false
	}
	ctx, cancel := context.WithTimeout(context.Background(), server.DefaultConnectTimeout)
	defer cancel()
	if s.bucket != "" {
		_, err := s.client.HeadBucket(ctx, &s3.HeadBucketInput{Bucket: aws.String(s.bucket)})
		return err == nil
	}
	_, err := s.client.ListBuckets(ctx, &s3.ListBucketsInput{})
	return err == nil
}

// Put uploads an object to S3
func (s *S3Storage) Put(bucket, fnm string, binary []byte, tenantID ...string) error {
	if s.client == nil {
		return fmt.Errorf("storage client is not initialized")
	}
	bucket, fnm = s.resolveBucketAndPath(bucket, fnm)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if s.bucket == "" {
		_, err := s.client.HeadBucket(ctx, &s3.HeadBucketInput{Bucket: aws.String(bucket)})
		if err != nil {
			if !isS3NotFound(err) {
				return fmt.Errorf("check bucket: %w", err)
			}
			if _, err := s.client.CreateBucket(ctx, &s3.CreateBucketInput{Bucket: aws.String(bucket)}); err != nil {
				return fmt.Errorf("create bucket: %w", err)
			}
		}
	}
	_, err := s.client.PutObject(ctx, &s3.PutObjectInput{Bucket: aws.String(bucket), Key: aws.String(fnm), Body: bytes.NewReader(binary)})
	if err != nil {
		return fmt.Errorf("put object: %w", err)
	}
	return nil
}

// Get retrieves an object from S3
func (s *S3Storage) Get(bucket, fnm string, tenantID ...string) ([]byte, error) {
	if s.client == nil {
		return nil, fmt.Errorf("storage client is not initialized")
	}
	bucket, fnm = s.resolveBucketAndPath(bucket, fnm)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	result, err := s.client.GetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(bucket), Key: aws.String(fnm)})
	if err != nil {
		return nil, fmt.Errorf("get object: %w", err)
	}
	defer result.Body.Close()
	buf := new(bytes.Buffer)
	if _, err := buf.ReadFrom(result.Body); err != nil {
		return nil, fmt.Errorf("read object: %w", err)
	}
	return buf.Bytes(), nil
}

// Remove removes an object from S3
func (s *S3Storage) Remove(bucket, fnm string, tenantID ...string) error {
	bucket, fnm = s.resolveBucketAndPath(bucket, fnm)

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	_, err := s.client.DeleteObject(ctx, &s3.DeleteObjectInput{
		Bucket: aws.String(bucket),
		Key:    aws.String(fnm),
	})
	if err != nil {
		zap.L().Error("Failed to remove object", zap.String("bucket", bucket), zap.String("key", fnm), zap.Error(err))
		return err
	}

	return nil
}

// ObjExist checks if an object exists in S3
func (s *S3Storage) ObjExist(bucket, fnm string, tenantID ...string) bool {
	if s.client == nil {
		return false
	}

	bucket, fnm = s.resolveBucketAndPath(bucket, fnm)

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	_, err := s.client.HeadObject(ctx, &s3.HeadObjectInput{
		Bucket: aws.String(bucket),
		Key:    aws.String(fnm),
	})
	if err != nil {
		if isS3NotFound(err) {
			return false
		}
		return false
	}

	return true
}

// GetPresignedURL generates a presigned URL for accessing an object
func (s *S3Storage) GetPresignedURL(bucket, fnm string, expires time.Duration, tenantID ...string) (string, error) {
	if s.client == nil {
		return "", fmt.Errorf("storage client is not initialized")
	}
	bucket, fnm = s.resolveBucketAndPath(bucket, fnm)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	result, err := s3.NewPresignClient(s.client).PresignGetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(bucket), Key: aws.String(fnm)}, s3.WithPresignExpires(expires))
	if err != nil {
		return "", fmt.Errorf("presign object: %w", err)
	}
	return result.URL, nil
}

// BucketExists checks if a bucket exists
func (s *S3Storage) BucketExists(bucket string) bool {
	if s.client == nil {
		return false
	}

	actualBucket := bucket
	if s.bucket != "" {
		actualBucket = s.bucket
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	_, err := s.client.HeadBucket(ctx, &s3.HeadBucketInput{
		Bucket: aws.String(actualBucket),
	})
	if err != nil {
		zap.L().Debug("Bucket does not exist or error", zap.String("bucket", actualBucket), zap.Error(err))
		return false
	}

	return true
}

// RemoveBucket removes a bucket and all its objects
func (s *S3Storage) RemoveBucket(bucket string) error {
	if s.client == nil {
		return fmt.Errorf("storage client is not initialized")
	}
	actualBucket, prefix := s.resolveBucketAndPath(bucket, "")
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	input := &s3.ListObjectsV2Input{Bucket: aws.String(actualBucket), Prefix: aws.String(prefix)}
	for {
		result, err := s.client.ListObjectsV2(ctx, input)
		if err != nil {
			return fmt.Errorf("list objects: %w", err)
		}
		for _, object := range result.Contents {
			if _, err := s.client.DeleteObject(ctx, &s3.DeleteObjectInput{Bucket: aws.String(actualBucket), Key: object.Key}); err != nil {
				return fmt.Errorf("delete object: %w", err)
			}
		}
		if result.IsTruncated == nil || !*result.IsTruncated {
			break
		}
		if result.NextContinuationToken == nil || (input.ContinuationToken != nil && *input.ContinuationToken == *result.NextContinuationToken) {
			return fmt.Errorf("invalid object continuation token")
		}
		input.ContinuationToken = result.NextContinuationToken
	}
	if s.bucket == "" && s.prefixPath == "" {
		_, err := s.client.DeleteBucket(ctx, &s3.DeleteBucketInput{Bucket: aws.String(actualBucket)})
		return err
	}
	return nil
}

// Copy copies an object from source to destination
func (s *S3Storage) Copy(srcBucket, srcPath, destBucket, destPath string) bool {
	srcBucket, srcPath = s.resolveBucketAndPath(srcBucket, srcPath)
	destBucket, destPath = s.resolveBucketAndPath(destBucket, destPath)

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	copySource := fmt.Sprintf("%s/%s", srcBucket, srcPath)

	_, err := s.client.CopyObject(ctx, &s3.CopyObjectInput{
		Bucket:     aws.String(destBucket),
		Key:        aws.String(destPath),
		CopySource: aws.String(copySource),
	})
	if err != nil {
		zap.L().Error("Failed to copy object", zap.String("src", copySource), zap.String("dest", fmt.Sprintf("%s/%s", destBucket, destPath)), zap.Error(err))
		return false
	}

	return true
}

// Move moves an object from source to destination
func (s *S3Storage) Move(srcBucket, srcPath, destBucket, destPath string) bool {
	if s.Copy(srcBucket, srcPath, destBucket, destPath) {
		if err := s.Remove(srcBucket, srcPath); err != nil {
			zap.L().Error("Failed to remove source object after copy", zap.String("bucket", srcBucket), zap.String("key", srcPath), zap.Error(err))
			return false
		}
		return true
	}
	return false
}

// isNotFound checks if the error is a not found error
func isS3NotFound(err error) bool {
	if err == nil {
		return false
	}
	var apiErr smithy.APIError
	if errors.As(err, &apiErr) {
		return apiErr.ErrorCode() == "NotFound" || apiErr.ErrorCode() == "404" || apiErr.ErrorCode() == "NoSuchKey" || apiErr.ErrorCode() == "NoSuchBucket"
	}
	return false
}
