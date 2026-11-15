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

// OSSStorage implements Storage interface for Aliyun OSS
// OSS uses S3-compatible API
type OSSStorage struct {
	client     *s3.Client
	bucket     string
	prefixPath string
	config     *server.OSSConfig
}

// NewOSSStorage creates a new OSS storage instance
func NewOSSStorage(config *server.OSSConfig) (*OSSStorage, error) {
	if config == nil {
		return nil, fmt.Errorf("oss configuration is required")
	}
	configCopy := *config
	config = &configCopy

	storage := &OSSStorage{
		bucket:     config.Bucket,
		prefixPath: config.PrefixPath,
		config:     config,
	}

	if err := storage.connect(); err != nil {
		return nil, err
	}

	return storage, nil
}

func (o *OSSStorage) connect() error {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	// Create static credentials
	creds := credentials.NewStaticCredentialsProvider(
		o.config.AccessKey,
		o.config.SecretKey,
		"",
	)

	// Load configuration
	cfg, err := config.LoadDefaultConfig(ctx,
		config.WithRegion(o.config.Region),
		config.WithCredentialsProvider(creds),
	)
	if err != nil {
		return fmt.Errorf("failed to load OSS config: %w", err)
	}

	// Create S3 client with OSS endpoint
	o.client = s3.NewFromConfig(cfg, func(opts *s3.Options) {
		opts.BaseEndpoint = aws.String(o.config.EndpointURL)
	})

	return nil
}

func (o *OSSStorage) reconnect() {
	if err := o.connect(); err != nil {
		zap.L().Error("Failed to reconnect to OSS", zap.Error(err))
	}
}

func (o *OSSStorage) resolveBucketAndPath(bucket, fnm string) (string, string) {
	actualBucket := bucket
	if o.bucket != "" {
		actualBucket = o.bucket
	}
	actualPath := fnm
	if o.bucket != "" {
		actualPath = fmt.Sprintf("%s/%s", bucket, fnm)
	}
	if o.prefixPath != "" {
		actualPath = fmt.Sprintf("%s/%s", o.prefixPath, actualPath)
	}
	return actualBucket, actualPath
}

// Health checks OSS service availability
func (o *OSSStorage) Health() bool {
	if o.client == nil {
		return false
	}
	ctx, cancel := context.WithTimeout(context.Background(), server.DefaultConnectTimeout)
	defer cancel()
	if o.bucket != "" {
		_, err := o.client.HeadBucket(ctx, &s3.HeadBucketInput{Bucket: aws.String(o.bucket)})
		return err == nil
	}
	_, err := o.client.ListBuckets(ctx, &s3.ListBucketsInput{})
	return err == nil
}

// Put uploads an object to OSS
func (o *OSSStorage) Put(bucket, fnm string, binary []byte, tenantID ...string) error {
	if o.client == nil {
		return fmt.Errorf("storage client is not initialized")
	}
	bucket, fnm = o.resolveBucketAndPath(bucket, fnm)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if o.bucket == "" {
		_, err := o.client.HeadBucket(ctx, &s3.HeadBucketInput{Bucket: aws.String(bucket)})
		if err != nil {
			if !isOSSNotFound(err) {
				return fmt.Errorf("check bucket: %w", err)
			}
			if _, err := o.client.CreateBucket(ctx, &s3.CreateBucketInput{Bucket: aws.String(bucket)}); err != nil {
				return fmt.Errorf("create bucket: %w", err)
			}
		}
	}
	_, err := o.client.PutObject(ctx, &s3.PutObjectInput{Bucket: aws.String(bucket), Key: aws.String(fnm), Body: bytes.NewReader(binary)})
	if err != nil {
		return fmt.Errorf("put object: %w", err)
	}
	return nil
}

// Get retrieves an object from OSS
func (o *OSSStorage) Get(bucket, fnm string, tenantID ...string) ([]byte, error) {
	if o.client == nil {
		return nil, fmt.Errorf("storage client is not initialized")
	}
	bucket, fnm = o.resolveBucketAndPath(bucket, fnm)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	result, err := o.client.GetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(bucket), Key: aws.String(fnm)})
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

// Remove removes an object from OSS
func (o *OSSStorage) Remove(bucket, fnm string, tenantID ...string) error {
	bucket, fnm = o.resolveBucketAndPath(bucket, fnm)

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	_, err := o.client.DeleteObject(ctx, &s3.DeleteObjectInput{
		Bucket: aws.String(bucket),
		Key:    aws.String(fnm),
	})
	if err != nil {
		zap.L().Error("Failed to remove object", zap.String("bucket", bucket), zap.String("key", fnm), zap.Error(err))
		return err
	}

	return nil
}

// ObjExist checks if an object exists in OSS
func (o *OSSStorage) ObjExist(bucket, fnm string, tenantID ...string) bool {
	if o.client == nil {
		return false
	}

	bucket, fnm = o.resolveBucketAndPath(bucket, fnm)

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	_, err := o.client.HeadObject(ctx, &s3.HeadObjectInput{
		Bucket: aws.String(bucket),
		Key:    aws.String(fnm),
	})
	if err != nil {
		if isOSSNotFound(err) {
			return false
		}
		return false
	}

	return true
}

// GetPresignedURL generates a presigned URL for accessing an object
func (o *OSSStorage) GetPresignedURL(bucket, fnm string, expires time.Duration, tenantID ...string) (string, error) {
	if o.client == nil {
		return "", fmt.Errorf("storage client is not initialized")
	}
	bucket, fnm = o.resolveBucketAndPath(bucket, fnm)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	result, err := s3.NewPresignClient(o.client).PresignGetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(bucket), Key: aws.String(fnm)}, s3.WithPresignExpires(expires))
	if err != nil {
		return "", fmt.Errorf("presign object: %w", err)
	}
	return result.URL, nil
}

// BucketExists checks if a bucket exists
func (o *OSSStorage) BucketExists(bucket string) bool {
	if o.client == nil {
		return false
	}

	actualBucket := bucket
	if o.bucket != "" {
		actualBucket = o.bucket
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	_, err := o.client.HeadBucket(ctx, &s3.HeadBucketInput{
		Bucket: aws.String(actualBucket),
	})
	if err != nil {
		zap.L().Debug("Bucket does not exist or error", zap.String("bucket", actualBucket), zap.Error(err))
		return false
	}

	return true
}

// RemoveBucket removes a bucket and all its objects
func (o *OSSStorage) RemoveBucket(bucket string) error {
	if o.client == nil {
		return fmt.Errorf("storage client is not initialized")
	}
	actualBucket, prefix := o.resolveBucketAndPath(bucket, "")
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	input := &s3.ListObjectsV2Input{Bucket: aws.String(actualBucket), Prefix: aws.String(prefix)}
	for {
		result, err := o.client.ListObjectsV2(ctx, input)
		if err != nil {
			return fmt.Errorf("list objects: %w", err)
		}
		for _, object := range result.Contents {
			if _, err := o.client.DeleteObject(ctx, &s3.DeleteObjectInput{Bucket: aws.String(actualBucket), Key: object.Key}); err != nil {
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
	if o.bucket == "" && o.prefixPath == "" {
		_, err := o.client.DeleteBucket(ctx, &s3.DeleteBucketInput{Bucket: aws.String(actualBucket)})
		return err
	}
	return nil
}

// Copy copies an object from source to destination
func (o *OSSStorage) Copy(srcBucket, srcPath, destBucket, destPath string) bool {
	srcBucket, srcPath = o.resolveBucketAndPath(srcBucket, srcPath)
	destBucket, destPath = o.resolveBucketAndPath(destBucket, destPath)

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	copySource := fmt.Sprintf("%s/%s", srcBucket, srcPath)

	_, err := o.client.CopyObject(ctx, &s3.CopyObjectInput{
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
func (o *OSSStorage) Move(srcBucket, srcPath, destBucket, destPath string) bool {
	if o.Copy(srcBucket, srcPath, destBucket, destPath) {
		if err := o.Remove(srcBucket, srcPath); err != nil {
			zap.L().Error("Failed to remove source object after copy", zap.String("bucket", srcBucket), zap.String("key", srcPath), zap.Error(err))
			return false
		}
		return true
	}
	return false
}

// Helper functions
func isOSSNotFound(err error) bool {
	if err == nil {
		return false
	}
	var apiErr smithy.APIError
	if errors.As(err, &apiErr) {
		return apiErr.ErrorCode() == "NotFound" || apiErr.ErrorCode() == "404" || apiErr.ErrorCode() == "NoSuchKey" || apiErr.ErrorCode() == "NoSuchBucket"
	}
	return false
}
