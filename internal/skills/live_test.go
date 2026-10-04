package skills

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	mc "github.com/milvus-io/milvus/client/v2/milvusclient"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	gormlog "gorm.io/gorm/logger"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
)

// TestSkillsLiveServer is driven by a private Python scratch fixture; no business database is accepted.
func TestSkillsLiveServer(t *testing.T) {
	path := os.Getenv("MULTIRAG_SKILLS_LIVE_CONFIG")
	if path == "" {
		t.Skip("requires isolated Python harness")
	}
	raw, e := os.ReadFile(path)
	if e != nil {
		t.Fatal(e)
	}
	var cfg struct {
		Database  server.DatabaseConfig
		SecretKey string
		Milvus    mc.ClientConfig
		Minio     server.MinioConfig
	}
	if e = json.Unmarshal(raw, &cfg); e != nil {
		t.Fatal(e)
	}
	if len(cfg.Database.Database) < 14 || cfg.Database.Database[:14] != "multirag_test_" {
		t.Fatal("scratch database required")
	}
	dsn, e := dao.PostgresDSN(cfg.Database)
	if e != nil {
		t.Fatal("invalid scratch DSN")
	}
	db, e := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: gormlog.Default.LogMode(gormlog.Silent)})
	if e != nil {
		t.Fatal("scratch connection failed")
	}
	pool, e := db.DB()
	if e != nil {
		t.Fatal(e)
	}
	defer pool.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	index, e := NewMilvus(ctx, &cfg.Milvus)
	if e != nil {
		t.Fatal("Milvus connection failed")
	}
	defer index.Client.Close(ctx)
	blobs, e := NewObjectStore(&cfg.Minio)
	if e != nil {
		t.Fatal(e)
	}
	s := New(db, blobs, index, &LegacyModels{DB: db, Providers: &entity.ProviderManager{}})
	if e = s.Ready(ctx); e != nil {
		t.Fatal(e)
	}
	t.Run("lease_fencing", func(t *testing.T) { checkLiveFences(t, s) })
	t.Run("orphan_generations", func(t *testing.T) { checkLiveOrphanGenerations(t, s) })
	t.Run("managed_files", func(t *testing.T) { checkLiveFileGuards(t, s) })
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	router.Use(gin.Recovery())
	s.Register(router, func() string { return cfg.SecretKey })
	listener, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	httpServer := &http.Server{Handler: router}
	go httpServer.Serve(listener)
	defer httpServer.Close()
	go s.Run(ctx)
	if e = os.WriteFile(filepath.Join(filepath.Dir(path), "base"), []byte("http://"+listener.Addr().String()), 0600); e != nil {
		t.Fatal(e)
	}
	deadline := time.Now().Add(10 * time.Minute)
	for time.Now().Before(deadline) {
		if _, e = os.Stat(filepath.Join(filepath.Dir(path), "stop")); e == nil {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatal("harness deadline exceeded")
}
