package handler_test

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	mc "github.com/milvus-io/milvus/client/v2/milvusclient"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	gormlog "gorm.io/gorm/logger"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/logger"
	"multirag/internal/server"
	"multirag/internal/service"
	"multirag/internal/skills"
	"multirag/internal/storage"
)

func TestSkillCoreLiveServer(t *testing.T) {
	path := os.Getenv("MULTIRAG_SKILL_CORE_CONFIG")
	if path == "" {
		t.Skip("requires isolated scratch fixture")
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
	if !strings.HasPrefix(cfg.Database.Database, "multirag_test_") {
		t.Fatal("scratch required")
	}
	dsn, e := dao.PostgresDSN(cfg.Database)
	if e != nil {
		t.Fatal("invalid DSN")
	}
	db, e := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: gormlog.Default.LogMode(gormlog.Silent)})
	if e != nil {
		t.Fatal("scratch connection failed")
	}
	dao.DB = db
	pool, _ := db.DB()
	defer pool.Close()
	_ = logger.Init("error")
	server.SetSecretKey(cfg.SecretKey)
	blobs, e := skills.NewObjectStore(&cfg.Minio)
	if e != nil {
		t.Fatal(e)
	}
	plain, e := storage.NewMinioStorage(&cfg.Minio)
	if e != nil {
		t.Fatal(e)
	}
	storage.GetStorageFactory().SetStorage(plain)
	config := &server.Config{DocEngine: server.DocEngineConfig{Type: server.EngineMilvus, Milvus: &server.MilvusConfig{Hosts: cfg.Milvus.Address, Username: cfg.Milvus.Username, Password: cfg.Milvus.Password, DBName: cfg.Milvus.DBName}}}
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery())
	auth := skills.New(db, nil, nil, nil)
	models := &skills.LegacyModels{DB: db, Providers: &entity.ProviderManager{}}
	stop, e := handler.AttachSkillCore(r, config, auth.Authenticate, func(ctx context.Context, tenant string) (any, error) { return models.List(ctx, tenant) }, &coreFaultStore{blobs, filepath.Join(filepath.Dir(path), "fail-delete")})
	if e != nil {
		t.Fatal(e)
	}
	defer stop()
	files := r.Group("/api/v1/files")
	files.Use(func(c *gin.Context) {
		tenant, e := auth.Authenticate(c.Request.Context(), c.GetHeader("Authorization"), cfg.SecretKey)
		if e != nil {
			c.AbortWithStatus(401)
			return
		}
		c.Set("user", &entity.User{ID: tenant})
	})
	fh := handler.NewFileHandler(service.NewFileService(), service.NewUserService())
	files.GET("", fh.ListFiles)
	files.POST("", fh.UploadFile)
	files.DELETE("", fh.DeleteFiles)
	files.POST("/move", fh.MoveFiles)
	files.GET("/:id", fh.Download)
	listener, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	httpServer := &http.Server{Handler: r}
	go httpServer.Serve(listener)
	defer httpServer.Close()
	if e = os.WriteFile(filepath.Join(filepath.Dir(path), "base"), []byte("http://"+listener.Addr().String()), 0600); e != nil {
		t.Fatal(e)
	}
	deadline := time.Now().Add(45 * time.Minute)
	for time.Now().Before(deadline) {
		if _, e = os.Stat(filepath.Join(filepath.Dir(path), "stop")); e == nil {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatal("harness timed out")
}

type coreFaultStore struct {
	*skills.ObjectStore
	flag string
}

func (s *coreFaultStore) Remove(bucket, key string, args ...string) error {
	if _, e := os.Stat(s.flag); e == nil {
		return fmt.Errorf("fixture storage deletion unavailable")
	}
	return s.ObjectStore.Remove(bucket, key, args...)
}
