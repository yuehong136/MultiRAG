// Package scratch owns isolated SQL, Redis and object resources for API tests.
package scratch

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	"github.com/redis/go-redis/v9"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/dao"
	"multirag/internal/server"
	"multirag/internal/storage"
	"multirag/internal/testsupport"
)

type Config struct {
	Database      server.DatabaseConfig
	Redis         server.RedisConfig
	MinIO         server.MinioConfig
	Elasticsearch server.ElasticsearchConfig
}

type Services struct {
	DB       *gorm.DB
	Read     *sql.DB
	Redis    *redis.Client
	Journal  *testsupport.CaseJournal
	Store    *Objects
	Prefix   string
	Database string
	Bucket   string
	Config   Config
}

type Objects struct {
	storage.Storage
	journal    *testsupport.CaseJournal
	mu         sync.Mutex
	keys       map[string]bool
	PutFailure bool
	Calls      int
}

func (s *Objects) reserve(bucket, key string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	identity := bucket + "/" + key
	if !s.keys[identity] {
		s.journal.Event("reserve-object", map[string]any{"logical_bucket": bucket, "logical_key": key})
		s.keys[identity] = true
	}
	s.Calls++
}
func (s *Objects) Put(bucket, key string, data []byte, tenant ...string) error {
	s.reserve(bucket, key)
	err := s.Storage.Put(bucket, key, data, tenant...)
	if err == nil && s.PutFailure {
		return fmt.Errorf("controlled persisted object failure")
	}
	return err
}
func (s *Objects) Get(bucket, key string, tenant ...string) ([]byte, error) {
	s.reserve(bucket, key)
	return s.Storage.Get(bucket, key, tenant...)
}

func (s *Objects) GetContext(ctx context.Context, bucket, key string, tenant ...string) ([]byte, error) {
	s.reserve(bucket, key)
	return s.Storage.(storage.ContextReader).GetContext(ctx, bucket, key, tenant...)
}
func (s *Objects) ObjectExistsContext(ctx context.Context, bucket, key string) (bool, error) {
	s.reserve(bucket, key)
	return s.Storage.(interface {
		ObjectExistsContext(context.Context, string, string) (bool, error)
	}).ObjectExistsContext(ctx, bucket, key)
}
func (s *Objects) RemoveContext(ctx context.Context, bucket, key string, tenant ...string) error {
	s.reserve(bucket, key)
	return s.Storage.(interface {
		RemoveContext(context.Context, string, string, ...string) error
	}).RemoveContext(ctx, bucket, key, tenant...)
}

func New(t testing.TB) *Services {
	t.Helper()
	path := os.Getenv("MULTIRAG_AFTERDCF_G_CONFIG")
	if path == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("private scratch services config required")
		}
		t.Skip("requires isolated PostgreSQL/Redis/MinIO")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("read private scratch config")
	}
	var cfg Config
	if json.Unmarshal(raw, &cfg) != nil {
		t.Fatal("invalid private scratch config")
	}
	if !loopbackHost(cfg.Database.Host) || cfg.Database.Port < 1 || cfg.Database.Port > 65535 || cfg.Database.Database != "postgres" || !loopbackHost(cfg.Redis.Host) || cfg.Redis.Port < 1 || cfg.Redis.Port > 65535 || cfg.Redis.DB < 0 {
		t.Fatal("loopback admin/isolated Redis configuration required")
	}
	host, _, err := net.SplitHostPort(cfg.MinIO.Host)
	if err != nil || !loopbackHost(host) || cfg.MinIO.Bucket != "" || cfg.MinIO.PrefixPath != "" {
		t.Fatal("unprefixed loopback object config required")
	}
	j := testsupport.NewCase(t)
	prefix := "gdcf" + strings.ReplaceAll(uuid.NewString(), "-", "")[:12]
	name := "multirag_afterdcf_" + prefix
	bucket := "multirag-afterdcf-" + prefix
	j.Event("reserve-services", map[string]any{"database": name, "sql_endpoint": net.JoinHostPort(cfg.Database.Host, fmt.Sprint(cfg.Database.Port)), "schema": "usr_ai", "redis_endpoint": net.JoinHostPort(cfg.Redis.Host, fmt.Sprint(cfg.Redis.Port)), "redis_db": cfg.Redis.DB, "redis_owned_key_pattern": "*" + prefix + "*", "object_endpoint": cfg.MinIO.Host, "physical_bucket": bucket, "config_file": path})
	dsn, err := dao.PostgresDSN(cfg.Database)
	if err != nil {
		t.Fatal("admin DSN")
	}
	admin, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("admin connection")
	}
	t.Cleanup(func() { admin.Close() })
	var present bool
	if err = admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&present); err != nil || present {
		t.Fatalf("scratch database precondition: %v", err)
	}
	t.Cleanup(func() {
		if _, e := admin.Exec(`DROP DATABASE IF EXISTS "` + name + `" WITH (FORCE)`); e != nil {
			t.Error("drop scratch database")
		}
		var exists bool
		e := admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&exists)
		j.Event("cleanup-database", map[string]any{"database": name, "absent": e == nil && !exists})
		if e != nil || exists {
			t.Error("independent database cleanup readback")
		}
	})
	if _, err = admin.Exec(`CREATE DATABASE "` + name + `"`); err != nil {
		t.Fatal("create scratch database")
	}
	cfg.Database.Database = name
	cfg.Database.Schema = "usr_ai"
	dsn, err = dao.PostgresDSN(cfg.Database)
	if err != nil {
		t.Fatal("scratch DSN")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("scratch connection")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	if err = db.Exec("CREATE SCHEMA usr_ai").Error; err != nil {
		t.Fatal("scratch schema")
	}
	read, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("readback connection")
	}
	t.Cleanup(func() { read.Close() })
	options := &redis.Options{Addr: net.JoinHostPort(cfg.Redis.Host, fmt.Sprint(cfg.Redis.Port)), Password: cfg.Redis.Password, DB: cfg.Redis.DB}
	client := redis.NewClient(options)
	t.Cleanup(func() { client.Close() })
	if client.Ping(t.Context()).Err() != nil {
		t.Fatal("scratch Redis unavailable")
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		reader := redis.NewClient(options)
		defer reader.Close()
		keys := []string{}
		iter := reader.Scan(ctx, 0, "*"+prefix+"*", 100).Iterator()
		for iter.Next(ctx) {
			keys = append(keys, iter.Val())
		}
		if iter.Err() != nil {
			t.Error("Redis owned key enumeration")
		}
		if len(keys) > 0 {
			if client.Del(ctx, keys...).Err() != nil {
				t.Error("Redis owned key cleanup")
			}
		}
		var remaining int64
		if len(keys) > 0 {
			remaining, err = reader.Exists(ctx, keys...).Result()
		}
		j.Event("cleanup-redis", map[string]any{"owned_key_pattern": "*" + prefix + "*", "keys": keys, "remaining": remaining, "confirmed": err == nil && remaining == 0})
		if err != nil || remaining != 0 {
			t.Error("independent Redis cleanup readback")
		}
	})
	newMinio := func() *minio.Client {
		c, e := minio.New(cfg.MinIO.Host, &minio.Options{Creds: credentials.NewStaticV4(cfg.MinIO.User, cfg.MinIO.Password, ""), Secure: cfg.MinIO.Secure, Region: cfg.MinIO.Region})
		if e != nil {
			t.Fatal("object client")
		}
		return c
	}
	objects := newMinio()
	if present, err = objects.BucketExists(t.Context(), bucket); err != nil || present {
		t.Fatal("scratch bucket precondition")
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		reader := newMinio()
		for object := range reader.ListObjects(ctx, bucket, minio.ListObjectsOptions{Recursive: true}) {
			if object.Err != nil {
				t.Error("list owned bucket")
				break
			}
			if objects.RemoveObject(ctx, bucket, object.Key, minio.RemoveObjectOptions{}) != nil {
				t.Error("remove owned object")
			}
		}
		if objects.RemoveBucket(ctx, bucket) != nil {
			t.Error("remove owned bucket")
		}
		exists, e := reader.BucketExists(ctx, bucket)
		j.Event("cleanup-bucket", map[string]any{"bucket": bucket, "absent": e == nil && !exists})
		if e != nil || exists {
			t.Error("independent bucket cleanup readback")
		}
	})
	if err = objects.MakeBucket(t.Context(), bucket, minio.MakeBucketOptions{}); err != nil {
		t.Fatal("create owned bucket")
	}
	cfg.MinIO.Bucket = bucket
	store, err := storage.NewMinioStorage(&cfg.MinIO)
	if err != nil {
		t.Fatal("owned storage adapter")
	}
	return &Services{DB: db, Read: read, Redis: client, Journal: j, Prefix: prefix, Database: name, Bucket: bucket, Config: cfg, Store: &Objects{Storage: store, journal: j, keys: map[string]bool{}}}
}

// ReadObject uses an independent SDK client and checks exact persisted bytes.
func (s *Services) ReadObject(t testing.TB, bucket, key string, want []byte) {
	t.Helper()
	client, err := minio.New(s.Config.MinIO.Host, &minio.Options{Creds: credentials.NewStaticV4(s.Config.MinIO.User, s.Config.MinIO.Password, ""), Secure: s.Config.MinIO.Secure})
	if err != nil {
		t.Fatal(err)
	}
	object, err := client.GetObject(t.Context(), s.Bucket, bucket+"/"+key, minio.GetObjectOptions{})
	if err != nil {
		t.Fatal(err)
	}
	defer object.Close()
	actual, err := io.ReadAll(object)
	if err != nil || !bytes.Equal(actual, want) {
		t.Fatal("independent object readback mismatch")
	}
}

func (s *Services) ObjectKeys(t testing.TB) []string {
	t.Helper()
	client, err := minio.New(s.Config.MinIO.Host, &minio.Options{Creds: credentials.NewStaticV4(s.Config.MinIO.User, s.Config.MinIO.Password, ""), Secure: s.Config.MinIO.Secure})
	if err != nil {
		t.Fatal(err)
	}
	keys := []string{}
	for object := range client.ListObjects(t.Context(), s.Bucket, minio.ListObjectsOptions{Recursive: true}) {
		if object.Err != nil {
			t.Fatal("independent object listing")
		}
		keys = append(keys, object.Key)
	}
	sort.Strings(keys)
	return keys
}

func loopbackHost(host string) bool {
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}
