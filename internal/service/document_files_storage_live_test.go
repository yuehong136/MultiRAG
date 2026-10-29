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
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	"github.com/redis/go-redis/v9"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/storage"
	"multirag/internal/testsupport"
)

type documentFilesExternalConfig struct {
	MinIO server.MinioConfig
	Redis struct {
		Host, Password string
		DB             int
	}
	Bucket string
}

func documentFilesReadExternalConfig(t *testing.T) (documentFilesExternalConfig, []byte) {
	t.Helper()
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
	var cfg documentFilesExternalConfig
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
	return cfg, raw
}

const documentFilesDeleteExactBinding = `if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end`

// The obligation starts before the remote call: an error can mean that the
// server committed the mutation and its acknowledgement never reached us.
type documentFilesExternalResources struct {
	journal                            *testsupport.CaseJournal
	bucket, redisKey                   string
	objectKeys                         []string
	binding                            []byte
	reader, cleanupReader              *minio.Client
	registry, registryReader           *redis.Client
	bucketAttempted, registryAttempted bool
}

func newDocumentFilesExternalResources(t *testing.T, cfg documentFilesExternalConfig, journal *testsupport.CaseJournal, bucket, redisKey string, binding []byte, objectKeys []string) *documentFilesExternalResources {
	t.Helper()
	newReader := func() *minio.Client {
		client, err := minio.New(cfg.MinIO.Host, &minio.Options{Creds: credentials.NewStaticV4(cfg.MinIO.User, cfg.MinIO.Password, ""), Secure: cfg.MinIO.Secure, Region: cfg.MinIO.Region})
		if err != nil {
			t.Fatal("independent F MinIO client unavailable")
		}
		return client
	}
	options := &redis.Options{Addr: cfg.Redis.Host, Password: cfg.Redis.Password, DB: cfg.Redis.DB}
	owned := &documentFilesExternalResources{journal: journal, bucket: bucket, redisKey: redisKey, binding: binding, objectKeys: objectKeys,
		reader: newReader(), cleanupReader: newReader(), registry: redis.NewClient(options), registryReader: redis.NewClient(options)}
	t.Cleanup(func() { _ = owned.registry.Close(); _ = owned.registryReader.Close() })
	t.Cleanup(func() {
		if err := owned.cleanup(); err != nil {
			t.Error(err)
		}
	})
	return owned
}

func (r *documentFilesExternalResources) createBucket(create func() error) error {
	r.journal.Event("F-bucket-create-attempt-before-call", map[string]any{"bucket": r.bucket})
	r.bucketAttempted = true
	return create()
}

func (r *documentFilesExternalResources) createRegistry(create func() (bool, error)) (bool, error) {
	r.journal.Event("F-registry-create-attempt-before-call", map[string]any{"key": r.redisKey, "binding_sha256": fmt.Sprintf("%x", sha256.Sum256(r.binding))})
	r.registryAttempted = true
	return create()
}

func (r *documentFilesExternalResources) cleanup() error {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	var failures []error
	if r.registryAttempted {
		if err := r.registry.Eval(ctx, documentFilesDeleteExactBinding, []string{r.redisKey}, string(r.binding)).Err(); err != nil {
			failures = append(failures, errors.New("owned F artifact registry compare-and-delete failed"))
		} else {
			value, err := r.registryReader.Get(ctx, r.redisKey).Bytes()
			absent, foreign := errors.Is(err, redis.Nil), err == nil && !bytes.Equal(value, r.binding)
			if !absent && !foreign {
				failures = append(failures, errors.New("independent F artifact registry cleanup readback failed"))
			} else {
				r.registryAttempted = false
			}
			r.journal.Event("F-artifact-registry-cleanup-independent-readback", map[string]any{"key": r.redisKey, "absent": absent, "foreign_value_preserved": foreign, "owned_binding_absent": absent || foreign})
		}
	}
	if r.bucketAttempted {
		exists, err := r.cleanupReader.BucketExists(ctx, r.bucket)
		if err != nil {
			failures = append(failures, errors.New("independent F MinIO bucket presence readback failed"))
		} else {
			if exists {
				for _, key := range r.objectKeys {
					if err := r.reader.RemoveObject(ctx, r.bucket, key, minio.RemoveObjectOptions{}); err != nil {
						failures = append(failures, errors.New("owned F MinIO object cleanup failed"))
					}
					if _, err := r.cleanupReader.StatObject(ctx, r.bucket, key, minio.StatObjectOptions{}); err == nil || !documentFilesMissingObject(err) {
						failures = append(failures, errors.New("independent F MinIO object cleanup readback failed"))
					}
				}
				if err := r.reader.RemoveBucket(ctx, r.bucket); err != nil {
					failures = append(failures, errors.New("owned F MinIO bucket cleanup failed"))
				}
			}
			exists, err = r.cleanupReader.BucketExists(ctx, r.bucket)
			if err != nil || exists {
				failures = append(failures, errors.New("independent F MinIO bucket cleanup readback failed"))
			} else {
				r.bucketAttempted = false
			}
			r.journal.Event("F-Minio-cleanup-independent-readback", map[string]any{"bucket": r.bucket, "absent": !exists && err == nil, "borrowed_service_preserved": true})
		}
	}
	return errors.Join(failures...)
}

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
	cfg, raw := documentFilesReadExternalConfig(t)
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
	objectKeys := make([]string, 0, len(objects))
	for key := range objects {
		objectKeys = append(objectKeys, key)
	}
	owned := newDocumentFilesExternalResources(t, cfg, f.journal, cfg.Bucket, redisKey, binding, objectKeys)
	reader, registry, registryReader := owned.reader, owned.registry, owned.registryReader
	exists, err := reader.BucketExists(ctx, cfg.Bucket)
	if err != nil || exists {
		t.Fatal("owned F MinIO bucket must be absent before creation")
	}
	if remaining, err := registryReader.Exists(ctx, redisKey).Result(); err != nil || remaining != 0 {
		t.Fatal("owned F artifact registry key must be absent before creation")
	}
	f.journal.Event("F-external-preexistence-independent-readback", map[string]any{"bucket_absent": true, "redis_key_absent": true})
	if err := owned.createBucket(func() error {
		return reader.MakeBucket(ctx, cfg.Bucket, minio.MakeBucketOptions{Region: cfg.MinIO.Region})
	}); err != nil {
		t.Fatal("owned F MinIO bucket creation failed")
	}
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
	if created, err := owned.createRegistry(func() (bool, error) { return registry.SetNX(ctx, redisKey, string(binding), 5*time.Minute).Result() }); err != nil || !created {
		t.Fatal("owned F artifact registry creation failed")
	}
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

func TestDocumentFilesResourceAckLoss(t *testing.T) {
	cfg, raw := documentFilesReadExternalConfig(t)
	journal := testsupport.NewCase(t)
	bucket := "afteraab-f-ackloss-20261029"
	filename := strings.ReplaceAll(uuid.NewString(), "-", "") + ".json"
	key := "multirag:sandbox_artifact:" + filename
	binding, err := json.Marshal(artifactBinding{OwnerID: "afteraab-f-ackloss", RunID: strings.ReplaceAll(uuid.NewString(), "-", ""), SessionID: nil})
	if err != nil {
		t.Fatal("owned acknowledgement-loss binding encoding failed")
	}
	foreign := []byte(`{"controlled_replacement":"preserve this exact value"}`)
	journal.Event("reserve-F-acknowledgement-loss-resources-before-first-access", map[string]any{
		"config_sha256": fmt.Sprintf("%x", sha256.Sum256(raw)), "minio_endpoint": cfg.MinIO.Host, "physical_bucket": bucket,
		"object_keys": []string{}, "redis_endpoint": cfg.Redis.Host, "redis_database": cfg.Redis.DB, "redis_key": key,
		"binding_sha256": fmt.Sprintf("%x", sha256.Sum256(binding)), "controlled_replacement_sha256": fmt.Sprintf("%x", sha256.Sum256(foreign)),
		"fault": "real mutation followed by a controlled lost-acknowledgement error",
	})
	owned := newDocumentFilesExternalResources(t, cfg, journal, bucket, key, binding, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if exists, err := owned.cleanupReader.BucketExists(ctx, bucket); err != nil || exists {
		t.Fatal("owned acknowledgement-loss bucket must initially be absent")
	}
	if remaining, err := owned.registryReader.Exists(ctx, key).Result(); err != nil || remaining != 0 {
		t.Fatal("owned acknowledgement-loss registry key must initially be absent")
	}
	journal.Event("F-acknowledgement-loss-preexistence-independent-readback", map[string]any{"bucket_absent": true, "redis_key_absent": true})
	ackLoss := errors.New("controlled remote acknowledgement loss")
	if err := owned.createBucket(func() error {
		if err := owned.reader.MakeBucket(ctx, bucket, minio.MakeBucketOptions{Region: cfg.MinIO.Region}); err != nil {
			return err
		}
		return ackLoss
	}); !errors.Is(err, ackLoss) || !owned.bucketAttempted {
		t.Fatal("bucket creation did not retain cleanup ownership after lost acknowledgement")
	}
	if created, err := owned.createRegistry(func() (bool, error) {
		if created, err := owned.registry.SetNX(ctx, key, string(binding), 5*time.Minute).Result(); err != nil || !created {
			return created, err
		}
		return false, ackLoss
	}); created || !errors.Is(err, ackLoss) || !owned.registryAttempted {
		t.Fatal("registry creation did not retain cleanup ownership after lost acknowledgement")
	}
	if exists, err := owned.cleanupReader.BucketExists(ctx, bucket); err != nil || !exists {
		t.Fatal("independent bucket readback did not observe the acknowledged-as-failed mutation")
	}
	if actual, err := owned.registryReader.Get(ctx, key).Bytes(); err != nil || !bytes.Equal(actual, binding) {
		t.Fatal("independent registry readback did not observe the acknowledged-as-failed mutation")
	}
	journal.Event("F-controlled-acknowledgement-loss-independent-readback", map[string]any{"bucket_exists_after_error": true, "exact_binding_exists_after_error": true})
	if err := owned.cleanup(); err != nil {
		t.Fatal("lost-acknowledgement resources were not cleaned")
	}
	if exists, err := owned.cleanupReader.BucketExists(ctx, bucket); err != nil || exists {
		t.Fatal("lost-acknowledgement bucket cleanup was not independently confirmed")
	}
	if remaining, err := owned.registryReader.Exists(ctx, key).Result(); err != nil || remaining != 0 {
		t.Fatal("lost-acknowledgement registry cleanup was not independently confirmed")
	}

	// A replacement is a separate registered test resource. The original
	// obligation must preserve it; only its own exact-value obligation removes it.
	if created, err := owned.createRegistry(func() (bool, error) { return owned.registry.SetNX(ctx, key, string(binding), 5*time.Minute).Result() }); err != nil || !created {
		t.Fatal("owned replacement-guard registry setup failed")
	}
	replacement := *owned
	replacement.binding = foreign
	replacement.registryAttempted = false
	t.Cleanup(func() {
		if err := replacement.cleanup(); err != nil {
			t.Error(err)
		}
	})
	if _, err := replacement.createRegistry(func() (bool, error) {
		err := owned.registry.Set(ctx, key, string(foreign), 5*time.Minute).Err()
		return err == nil, err
	}); err != nil {
		t.Fatal("owned controlled registry replacement failed")
	}
	if err := owned.cleanup(); err != nil {
		t.Fatal("original cleanup failed while preserving a different registry value")
	}
	if actual, err := owned.registryReader.Get(ctx, key).Bytes(); err != nil || !bytes.Equal(actual, foreign) {
		t.Fatal("original cleanup deleted a different registry value")
	}
	journal.Event("F-registry-foreign-value-preserved-independent-readback", map[string]any{"key": key, "replacement_sha256": fmt.Sprintf("%x", sha256.Sum256(foreign)), "preserved": true})
	if err := replacement.cleanup(); err != nil {
		t.Fatal("controlled registry replacement cleanup failed")
	}
	if remaining, err := owned.registryReader.Exists(ctx, key).Result(); err != nil || remaining != 0 {
		t.Fatal("controlled registry replacement absence was not independently confirmed")
	}
	journal.Event("F-acknowledgement-loss-final-independent-readback", map[string]any{"bucket": bucket, "bucket_absent": true, "key": key, "key_absent": true, "different_value_preserved_before_owned_cleanup": true})
}
