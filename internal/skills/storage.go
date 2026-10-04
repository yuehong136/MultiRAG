package skills

import (
	"bytes"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	"golang.org/x/crypto/pbkdf2"
	"multirag/internal/server"
)

// ObjectStore uses strict MinIO errors; the generic legacy adapter loses not-found information.
type ObjectStore struct {
	Client         *minio.Client
	Bucket, Prefix string
	crypto         cipher.Block
}

func NewObjectStore(cfg *server.MinioConfig) (*ObjectStore, error) {
	if cfg == nil {
		return nil, fault(503, "STORAGE_UNAVAILABLE")
	}
	endpoint := strings.TrimPrefix(strings.TrimPrefix(cfg.Host, "https://"), "http://")
	client, e := minio.New(endpoint, &minio.Options{Creds: credentials.NewStaticV4(cfg.User, cfg.Password, ""), Secure: cfg.Secure || strings.HasPrefix(cfg.Host, "https://"), Region: cfg.Region})
	if e != nil {
		return nil, e
	}
	s := &ObjectStore{Client: client, Bucket: cfg.Bucket, Prefix: cfg.PrefixPath}
	if strings.EqualFold(os.Getenv("MultiRAG_CRYPTO_ENABLED"), "true") {
		algorithm := os.Getenv("MultiRAG_CRYPTO_ALGORITHM")
		if algorithm == "" {
			algorithm = "aes-256-cbc"
		}
		size := 32
		if algorithm == "aes-128-cbc" {
			size = 16
		} else if algorithm != "aes-256-cbc" {
			return nil, fault(503, "STORAGE_CRYPTO_UNSUPPORTED")
		}
		key := os.Getenv("MultiRAG_CRYPTO_KEY")
		if key == "" {
			return nil, fault(503, "STORAGE_CRYPTO_UNAVAILABLE")
		}
		s.crypto, e = aes.NewCipher(pbkdf2.Key([]byte(key), []byte("multirag_crypto_salt"), 100000, size, sha256.New))
		if e != nil {
			return nil, e
		}
	}
	return s, nil
}
func (s *ObjectStore) resolve(bucket, key string) (string, string) {
	if s.Bucket != "" {
		key = bucket + "/" + key
		bucket = s.Bucket
	}
	if s.Prefix != "" {
		key = s.Prefix + "/" + key
	}
	return bucket, key
}
func (s *ObjectStore) encrypt(data []byte) ([]byte, error) {
	if s.crypto == nil {
		return data, nil
	}
	pad := aes.BlockSize - len(data)%aes.BlockSize
	padded := append(append([]byte{}, data...), bytes.Repeat([]byte{byte(pad)}, pad)...)
	out := make([]byte, 4+aes.BlockSize+len(padded))
	copy(out, "RAGF")
	iv := out[4:20]
	if _, e := rand.Read(iv); e != nil {
		return nil, e
	}
	cipher.NewCBCEncrypter(s.crypto, iv).CryptBlocks(out[20:], padded)
	return out, nil
}
func (s *ObjectStore) decrypt(data []byte) ([]byte, error) {
	if !bytes.HasPrefix(data, []byte("RAGF")) {
		return data, nil
	}
	if s.crypto == nil {
		return nil, fault(503, "STORAGE_CRYPTO_UNAVAILABLE")
	}
	if len(data) < 36 || (len(data)-20)%16 != 0 {
		return nil, fault(503, "CONTENT_INTEGRITY")
	}
	out := make([]byte, len(data)-20)
	cipher.NewCBCDecrypter(s.crypto, data[4:20]).CryptBlocks(out, data[20:])
	pad := int(out[len(out)-1])
	if pad < 1 || pad > 16 || !bytes.Equal(out[len(out)-pad:], bytes.Repeat([]byte{byte(pad)}, pad)) {
		return nil, fault(503, "CONTENT_INTEGRITY")
	}
	return out[:len(out)-pad], nil
}
func (s *ObjectStore) Put(bucket, key string, data []byte, _ ...string) error {
	bucket, key = s.resolve(bucket, key)
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	exists, e := s.Client.BucketExists(ctx, bucket)
	if e != nil {
		return e
	}
	if !exists {
		if e = s.Client.MakeBucket(ctx, bucket, minio.MakeBucketOptions{}); e != nil {
			exists, check := s.Client.BucketExists(ctx, bucket)
			if check != nil || !exists {
				return e
			}
		}
	}
	encrypted, e := s.encrypt(data)
	if e != nil {
		return e
	}
	_, e = s.Client.PutObject(ctx, bucket, key, bytes.NewReader(encrypted), int64(len(encrypted)), minio.PutObjectOptions{})
	return e
}
func (s *ObjectStore) Get(bucket, key string, _ ...string) ([]byte, error) {
	bucket, key = s.resolve(bucket, key)
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	obj, e := s.Client.GetObject(ctx, bucket, key, minio.GetObjectOptions{})
	if e != nil {
		return nil, e
	}
	defer obj.Close()
	data, e := readLimit(obj, maxFile+64)
	if e != nil {
		return nil, e
	}
	return s.decrypt(data)
}
func (s *ObjectStore) Exists(bucket, key string) (bool, error) {
	bucket, key = s.resolve(bucket, key)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	_, e := s.Client.StatObject(ctx, bucket, key, minio.StatObjectOptions{})
	if e == nil {
		return true, nil
	}
	code := minio.ToErrorResponse(e).Code
	if code == "NoSuchKey" || code == "NoSuchObject" || code == "NoSuchBucket" {
		return false, nil
	}
	return false, e
}
func (s *ObjectStore) Remove(bucket, key string, _ ...string) error {
	b, k := s.resolve(bucket, key)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if e := s.Client.RemoveObject(ctx, b, k, minio.RemoveObjectOptions{}); e != nil {
		return e
	}
	exists, e := s.Exists(bucket, key)
	if e != nil {
		return e
	}
	if exists {
		return fmt.Errorf("object deletion not visible")
	}
	return nil
}
