// Package scratch owns isolated SQL, Redis and object resources for API tests.
package scratch

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	mysqldriver "github.com/go-sql-driver/mysql"
	"github.com/google/uuid"
	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	"github.com/redis/go-redis/v9"
	gormmysql "gorm.io/driver/mysql"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/server"
	"multirag/internal/storage"
	"multirag/internal/testsupport"
)

type Config struct {
	Database      server.DatabaseConfig
	Redis         server.RedisConfig
	MinIO         server.MinioConfig
	Elasticsearch server.ElasticsearchConfig
	Nats          server.NatsConfig
	Infinity      server.InfinityConfig
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

func New(t testing.TB) *Services {
	t.Helper()
	path := os.Getenv("MULTIRAG_GO_ALIGNMENT_CONFIG")
	if path == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("private scratch services config required")
		}
		t.Fatal("requires isolated MySQL/Redis/MinIO")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("read private scratch config")
	}
	var cfg Config
	if json.Unmarshal(raw, &cfg) != nil {
		t.Fatal("invalid private scratch config")
	}
	if !loopbackHost(cfg.Database.Host) || cfg.Database.Port < 1 || cfg.Database.Port > 65535 || cfg.Database.Database != "mysql" || cfg.Database.Driver != "mysql" || !loopbackHost(cfg.Redis.Host) || cfg.Redis.Port < 1 || cfg.Redis.Port > 65535 || cfg.Redis.DB < 0 {
		t.Fatal("loopback admin/isolated Redis configuration required")
	}
	host, _, err := net.SplitHostPort(cfg.MinIO.Host)
	if err != nil || !loopbackHost(host) || cfg.MinIO.Bucket != "" || cfg.MinIO.PrefixPath != "" {
		t.Fatal("unprefixed loopback object config required")
	}
	j := testsupport.NewCase(t)
	prefix := "goa" + strings.ReplaceAll(uuid.NewString(), "-", "")[:12]
	name := "multirag_go_alignment_" + prefix
	bucket := "multirag-go-alignment-" + prefix
	leaseDirectory := filepath.Join(filepath.Dir(path), "redis-leases")
	if info, e := os.Stat(leaseDirectory); e != nil || !info.IsDir() {
		t.Fatal("pre-reserved Redis lease directory required")
	}
	j.Event("reserve-redis-db", map[string]any{"directory": leaseDirectory, "first": 16, "last": 1023, "owner": prefix})
	var leasePath string
	for index := 16; index <= 1023; index++ {
		candidate := filepath.Join(leaseDirectory, fmt.Sprintf("db-%d.lease", index))
		lease, e := os.OpenFile(candidate, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if errors.Is(e, os.ErrExist) {
			continue
		}
		if e != nil {
			t.Fatal("acquire Redis database lease")
		}
		_, e = lease.WriteString(prefix)
		closeErr := lease.Close()
		if e != nil || closeErr != nil {
			os.Remove(candidate)
			t.Fatal("write Redis database lease")
		}
		cfg.Redis.DB, leasePath = index, candidate
		break
	}
	if leasePath == "" {
		t.Fatal("isolated Redis database leases exhausted")
	}
	t.Cleanup(func() {
		if e := os.Remove(leasePath); e != nil {
			t.Error("release Redis database lease")
		}
	})
	j.Event("reserve-services", map[string]any{"database": name, "sql_endpoint": net.JoinHostPort(cfg.Database.Host, fmt.Sprint(cfg.Database.Port)), "redis_endpoint": net.JoinHostPort(cfg.Redis.Host, fmt.Sprint(cfg.Redis.Port)), "redis_db": cfg.Redis.DB, "redis_owned_key_pattern": "*" + prefix + "*", "object_endpoint": cfg.MinIO.Host, "physical_bucket": bucket, "config_file": path})
	dbConfig := mysqldriver.NewConfig()
	dbConfig.User, dbConfig.Passwd = cfg.Database.Username, cfg.Database.Password
	dbConfig.Net, dbConfig.Addr = "tcp", net.JoinHostPort(cfg.Database.Host, fmt.Sprint(cfg.Database.Port))
	dbConfig.DBName, dbConfig.ParseTime, dbConfig.Loc = "mysql", true, time.Local
	dbConfig.Timeout = 5 * time.Second
	dbConfig.ReadTimeout = 20 * time.Second
	dbConfig.WriteTimeout = 20 * time.Second
	dsn := dbConfig.FormatDSN()
	admin, err := sql.Open("mysql", dsn)
	if err != nil {
		t.Fatal("admin connection")
	}
	t.Cleanup(func() { admin.Close() })
	var present bool
	if err = admin.QueryRow("SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name=?)", name).Scan(&present); err != nil || present {
		t.Fatalf("scratch database precondition: %v", err)
	}
	t.Cleanup(func() {
		if _, e := admin.Exec("DROP DATABASE IF EXISTS `" + name + "`"); e != nil {
			t.Error("drop scratch database")
		}
		var exists bool
		e := admin.QueryRow("SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name=?)", name).Scan(&exists)
		j.Event("cleanup-database", map[string]any{"database": name, "absent": e == nil && !exists})
		if e != nil || exists {
			t.Error("independent database cleanup readback")
		}
	})
	if _, err = admin.Exec("CREATE DATABASE `" + name + "`"); err != nil {
		t.Fatal("create scratch database")
	}
	cfg.Database.Database = name
	dbConfig.DBName = name
	dsn = dbConfig.FormatDSN()
	db, err := gorm.Open(gormmysql.Open(dsn), &gorm.Config{TranslateError: true, Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("scratch connection")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	read, err := sql.Open("mysql", dsn)
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
	if count, e := client.DBSize(t.Context()).Result(); e != nil || count != 0 {
		t.Fatal("leased Redis database must be empty")
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		if e := client.FlushDB(ctx).Err(); e != nil {
			t.Error("cleanup owned Redis database")
		}
		reader := redis.NewClient(options)
		defer reader.Close()
		count, e := reader.DBSize(ctx).Result()
		j.Event("cleanup-redis", map[string]any{"database": cfg.Redis.DB, "remaining": count, "confirmed": e == nil && count == 0})
		if e != nil || count != 0 {
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
