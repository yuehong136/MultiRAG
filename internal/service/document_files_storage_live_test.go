package service

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"strconv"
	"testing"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	"github.com/redis/go-redis/v9"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/storage"
)

type documentFilesLiveObjects struct {
	*storage.MinioStorage
	gets, probes int
}

func (s *documentFilesLiveObjects) Get(bucket, key string, tenantID ...string) ([]byte, error) {
	s.gets++
	return s.MinioStorage.Get(bucket, key, tenantID...)
}

func (s *documentFilesLiveObjects) ObjectExistsContext(ctx context.Context, bucket, key string) (bool, error) {
	s.probes++
	return s.MinioStorage.ObjectExistsContext(ctx, bucket, key)
}

// TestDocumentFilesMinioRedis uses owned SQL rows, an initially absent bucket,
// and an exact Python artifact-registry key against real local backends.
func TestDocumentFilesMinioRedis(t *testing.T) {
	configPath := os.Getenv("MULTIRAG_AFTERAAB_F_EXTERNAL_CONFIG")
	if configPath == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned F MinIO/Redis configuration required")
		}
		t.Skip("owned F MinIO/Redis configuration required")
	}
	raw, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal("private F MinIO/Redis configuration unavailable")
	}
	var cfg struct {
		MinIO server.MinioConfig
		Redis struct {
			Host, Password string
			DB             int
		}
		Bucket string
	}
	if json.Unmarshal(raw, &cfg) != nil || cfg.Bucket != "afteraab-f-20261029" || cfg.MinIO.PrefixPath != "" || cfg.Redis.DB < 0 {
		t.Fatal("explicit owned F bucket and unprefixed local MinIO configuration required")
	}
	for _, endpoint := range []string{cfg.MinIO.Host, cfg.Redis.Host} {
		host, port, splitErr := net.SplitHostPort(endpoint)
		number, portErr := strconv.Atoi(port)
		if splitErr != nil || portErr != nil || number <= 0 || number > 65535 || (host != "127.0.0.1" && host != "localhost" && host != "::1") {
			t.Fatal("explicit local TCP MinIO/Redis endpoints required")
		}
	}
	f := newDocumentFilesFixture(t)
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	ids := f.ids
	filename, runID := ids["doc"]+".json", ids["link"]
	redisKey := "multirag:sandbox_artifact:" + filename
	source := []byte{0, 255, '\n', 's', 'o', 'u', 'r', 'c', 'e'}
	artifact := []byte(`{"code":102,"message":"these are artifact source bytes"}`)
	binding, err := json.Marshal(artifactBinding{OwnerID: ids["owner"], RunID: runID, SessionID: nil})
	if err != nil {
		t.Fatal("owned artifact binding encoding failed")
	}
	objects := map[string][]byte{
		ids["kb"] + "/source.bin":       source,
		ids["kb"] + "/empty.bin":        {},
		"sandbox-artifacts/" + filename: artifact,
	}
	missingKey := ids["kb"] + "/missing.bin"
	objectHashes := map[string]string{}
	for key, data := range objects {
		objectHashes[key] = fmt.Sprintf("%x", sha256.Sum256(data))
	}
	f.journal.Event("reserve-F-real-Minio-Redis-before-first-access", map[string]any{
		"config_sha256":  fmt.Sprintf("%x", sha256.Sum256(raw)),
		"minio_endpoint": cfg.MinIO.Host, "physical_bucket": cfg.Bucket,
		"object_sha256": objectHashes, "absent_object": missingKey,
		"document_namespace": ids["kb"], "artifact_namespace": "sandbox-artifacts",
		"redis_endpoint": cfg.Redis.Host, "redis_database": cfg.Redis.DB, "redis_key": redisKey,
		"binding_sha256": fmt.Sprintf("%x", sha256.Sum256(binding)),
		"owner_id":       ids["owner"], "run_id": runID, "session_id": nil,
	})
	newReader := func() *minio.Client {
		client, clientErr := minio.New(cfg.MinIO.Host, &minio.Options{Creds: credentials.NewStaticV4(cfg.MinIO.User, cfg.MinIO.Password, ""), Secure: cfg.MinIO.Secure, Region: cfg.MinIO.Region})
		if clientErr != nil {
			t.Fatal("independent F MinIO client unavailable")
		}
		return client
	}
	reader, cleanupReader := newReader(), newReader()
	redisOptions := &redis.Options{Addr: cfg.Redis.Host, Password: cfg.Redis.Password, DB: cfg.Redis.DB}
	registry, registryReader := redis.NewClient(redisOptions), redis.NewClient(redisOptions)
	t.Cleanup(func() { _ = registry.Close(); _ = registryReader.Close() })
	bucketCreated, registryCreated := false, false
	t.Cleanup(func() {
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cleanupCancel()
		if registryCreated {
			if err := registry.Del(cleanupCtx, redisKey).Err(); err != nil {
				t.Error("owned F artifact registry cleanup failed")
			}
			remaining, err := registryReader.Exists(cleanupCtx, redisKey).Result()
			if err != nil || remaining != 0 {
				t.Error("independent F artifact registry cleanup readback failed")
			}
			f.journal.Event("F-artifact-registry-cleanup-independent-readback", map[string]any{"key": redisKey, "remaining": remaining, "readback_succeeded": err == nil})
		}
		if bucketCreated {
			for key := range objects {
				if err := reader.RemoveObject(cleanupCtx, cfg.Bucket, key, minio.RemoveObjectOptions{}); err != nil {
					t.Error("owned F MinIO object cleanup failed")
				}
				if _, err := cleanupReader.StatObject(cleanupCtx, cfg.Bucket, key, minio.StatObjectOptions{}); err == nil || !documentFilesMissingObject(err) {
					t.Error("independent F MinIO object cleanup readback failed")
				}
			}
			if err := reader.RemoveBucket(cleanupCtx, cfg.Bucket); err != nil {
				t.Error("owned F MinIO bucket cleanup failed")
			}
			exists, err := cleanupReader.BucketExists(cleanupCtx, cfg.Bucket)
			if err != nil || exists {
				t.Error("independent F MinIO bucket cleanup readback failed")
			}
			f.journal.Event("F-Minio-cleanup-independent-readback", map[string]any{"bucket": cfg.Bucket, "absent": !exists && err == nil, "borrowed_service_preserved": true})
		}
	})
	exists, err := reader.BucketExists(ctx, cfg.Bucket)
	if err != nil || exists {
		t.Fatal("owned F MinIO bucket must be absent before creation")
	}
	if remaining, err := registryReader.Exists(ctx, redisKey).Result(); err != nil || remaining != 0 {
		t.Fatal("owned F artifact registry key must be absent before creation")
	}
	f.journal.Event("F-external-preexistence-independent-readback", map[string]any{"bucket_absent": true, "redis_key_absent": true})
	if err := reader.MakeBucket(ctx, cfg.Bucket, minio.MakeBucketOptions{Region: cfg.MinIO.Region}); err != nil {
		t.Fatal("owned F MinIO bucket creation failed")
	}
	bucketCreated = true
	cfg.MinIO.Bucket = cfg.Bucket
	store, err := storage.NewMinioStorage(&cfg.MinIO)
	if err != nil {
		t.Fatal("real F MinIO adapter unavailable")
	}
	counting := &documentFilesLiveObjects{MinioStorage: store}
	f.service.store = counting
	t.Setenv("SANDBOX_ARTIFACT_BUCKET", "sandbox-artifacts")
	for key, data := range objects {
		namespace, name, found := bytes.Cut([]byte(key), []byte("/"))
		if !found || store.Put(string(namespace), string(name), data) != nil {
			t.Fatal("owned F MinIO object creation failed")
		}
	}
	readObjects := func() {
		t.Helper()
		for key, expected := range objects {
			info, err := reader.StatObject(ctx, cfg.Bucket, key, minio.StatObjectOptions{})
			if err != nil || info.Size != int64(len(expected)) {
				t.Fatal("independent F MinIO object size readback failed")
			}
			object, err := reader.GetObject(ctx, cfg.Bucket, key, minio.GetObjectOptions{})
			if err != nil {
				t.Fatal("independent F MinIO bytes unavailable")
			}
			actual, readErr := io.ReadAll(object)
			closeErr := object.Close()
			if readErr != nil || closeErr != nil || !bytes.Equal(actual, expected) || sha256.Sum256(actual) != sha256.Sum256(expected) {
				t.Fatal("independent F MinIO source hash readback failed")
			}
		}
		if _, err := reader.StatObject(ctx, cfg.Bucket, missingKey, minio.StatObjectOptions{}); err == nil || !documentFilesMissingObject(err) {
			t.Fatal("independent F missing-object readback failed")
		}
	}
	readObjects()
	before := f.snapshot()
	for _, call := range []func() (*DocumentFile, error){
		func() (*DocumentFile, error) {
			return f.service.GetDocumentPreview(ctx, ids["normal"], ids["doc"], false)
		},
		func() (*DocumentFile, error) {
			return f.service.DownloadDocument(ctx, ids["owner"], ids["kb"], ids["doc"])
		},
	} {
		got, err := call()
		if err != nil || got == nil || !bytes.Equal(got.Data, source) || got.ContentType != "application/octet-stream" || got.FileName != "source.bin" {
			t.Fatal("real F MinIO document source read failed")
		}
	}
	if f.snapshot() != before {
		t.Fatal("real F binary source reads changed SQL rows")
	}
	for _, test := range []struct {
		name string
		want error
	}{{"empty.bin", nil}, {"missing.bin", ErrDocumentFileUnavailable}} {
		if err := f.db.Model(&entity.Document{}).Where("id=?", ids["doc"]).Update("location", test.name).Error; err != nil {
			t.Fatal("owned F source-address setup failed")
		}
		got, err := f.service.GetDocumentPreview(ctx, ids["normal"], ids["doc"], false)
		if !errors.Is(err, test.want) || test.want == nil && (got == nil || got.Data == nil || len(got.Data) != 0) || test.want != nil && got != nil {
			t.Fatal("real F MinIO empty and missing objects were conflated")
		}
	}
	if err := f.db.Model(&entity.Document{}).Where("id=?", ids["doc"]).Update("location", "source.bin").Error; err != nil {
		t.Fatal("owned F source-address restoration failed")
	}
	if created, err := registry.SetNX(ctx, redisKey, string(binding), 5*time.Minute).Result(); err != nil || !created {
		t.Fatal("owned F artifact registry creation failed")
	}
	registryCreated = true
	registered, err := registryReader.Get(ctx, redisKey).Bytes()
	if err != nil || !bytes.Equal(registered, binding) {
		t.Fatal("independent F artifact registry hash readback failed")
	}
	f.service.bindingReader = func(ctx context.Context, key string) (string, error) { return registry.Get(ctx, key).Result() }
	got, err := f.service.GetDocumentArtifact(ctx, ids["owner"], filename, runID, nil)
	if err != nil || got == nil || !bytes.Equal(got.Data, artifact) || got.ContentType != "application/json" {
		t.Fatal("real F artifact registry and MinIO source read failed")
	}
	before = f.snapshot()
	gets, probes := counting.gets, counting.probes
	for _, call := range []func() (*DocumentFile, error){
		func() (*DocumentFile, error) {
			return f.service.GetDocumentPreview(ctx, ids["outsider"], ids["doc"], false)
		},
		func() (*DocumentFile, error) {
			return f.service.GetDocumentPreview(ctx, ids["normal"], ids["doc"], true)
		},
		func() (*DocumentFile, error) {
			return f.service.DownloadDocument(ctx, ids["foreign-owner"], ids["kb"], ids["doc"])
		},
		func() (*DocumentFile, error) {
			return f.service.GetDocumentArtifact(ctx, ids["normal"], filename, runID, nil)
		},
	} {
		if result, err := call(); err == nil || result != nil || counting.gets != gets || counting.probes != probes {
			t.Fatal("denied F document/artifact access reached real object storage")
		}
	}
	if f.snapshot() != before {
		t.Fatal("denied real F binary access changed SQL rows")
	}
	registered, err = registryReader.Get(ctx, redisKey).Bytes()
	if err != nil || !bytes.Equal(registered, binding) {
		t.Fatal("denied real F binary access changed the artifact registry")
	}
	readObjects()
	f.journal.Event("F-real-Minio-Redis-independent-business-readback", map[string]any{
		"bucket": cfg.Bucket, "object_sha256": objectHashes, "empty_object_success": true, "missing_object_unavailable": true,
		"artifact_binding_sha256": fmt.Sprintf("%x", sha256.Sum256(registered)), "artifact_session_id": nil,
		"denied_object_GETs": 0, "denied_object_HEADs": 0, "SQL_unchanged_after_denial": true, "registry_unchanged_after_denial": true,
	})
}

func documentFilesMissingObject(err error) bool {
	code := minio.ToErrorResponse(err).Code
	return code == "NoSuchKey" || code == "NoSuchObject" || code == "NoSuchBucket"
}
