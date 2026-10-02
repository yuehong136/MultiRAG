package handler_test

import (
	"context"
	"database/sql"
	"encoding/json"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"multirag/internal/dao"
	"multirag/internal/handler"
	"multirag/internal/logger"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	gormlog "gorm.io/gorm/logger"
	"multirag/internal/engine"
	infengine "multirag/internal/engine/infinity"
	milvusengine "multirag/internal/engine/milvus"
)

type liveDocumentStatusConfig struct {
	SecretKey   string
	Database    server.DatabaseConfig
	InfinityURI string
	InfinityDB  string
	EngineType  server.EngineType
	Milvus      server.MilvusConfig
}

// This private signing key avoids writing the global system Redis secret.
// Requests below still use the real SQL API-token authentication fallback.
type statusSigningStore struct{ secret string }

func (s statusSigningStore) Get(string) (string, error)             { return s.secret, nil }
func (statusSigningStore) Set(string, string, time.Duration) bool   { return true }
func (statusSigningStore) SetNX(string, string, time.Duration) bool { return true }

func TestDocumentStatusLiveServer(t *testing.T) {
	configPath := os.Getenv("MULTIRAG_STATUS_LIVE_CONFIG")
	if configPath == "" {
		t.Skip("live harness is driven by the Python scratch integration fixture")
	}
	if err := logger.Init("error"); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	var cfg liveDocumentStatusConfig
	if err := json.Unmarshal(data, &cfg); err != nil {
		t.Fatal(err)
	}
	dsn, err := dao.PostgresDSN(cfg.Database)
	if err != nil {
		t.Fatal(err)
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: gormlog.Default.LogMode(gormlog.Silent)})
	if err != nil {
		t.Fatal("scratch PostgreSQL connection failed")
	}
	dao.DB = db
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	connections := []*sql.Conn{}
	for i := 0; i < 3; i++ {
		conn, err := pool.Conn(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		connections = append(connections, conn)
		var path string
		if err := conn.QueryRowContext(context.Background(), "SHOW search_path").Scan(&path); err != nil || !strings.Contains(path, "usr_ai") {
			t.Fatal("pooled schema mismatch")
		}
	}
	for _, conn := range connections {
		conn.Close()
	}
	t.Log("three concurrently held PostgreSQL connections resolve usr_ai")
	var store engine.DocEngine
	switch cfg.EngineType {
	case server.EngineInfinity:
		store, err = infengine.NewEngine(&server.InfinityConfig{URI: cfg.InfinityURI, DBName: cfg.InfinityDB})
	case server.EngineMilvus:
		store, err = milvusengine.NewEngine(&cfg.Milvus)
	}
	if err != nil {
		t.Fatal(err)
	}
	if store != nil {
		defer store.Close()
	}
	if err := server.InitVariables(statusSigningStore{secret: cfg.SecretKey}); err != nil {
		t.Fatal(err)
	}
	local.InitAdminStatus(0, "isolated task acceptance")
	gin.SetMode(gin.ReleaseMode)
	engine := gin.New()
	engine.Use(gin.Recovery())
	documentHandler := handler.NewDocumentHandler(service.NewDocumentStatusService(db, store, cfg.EngineType))
	appRouter := router.NewRouter(handler.NewAuthHandler(), nil, nil, documentHandler, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil)
	appRouter.Setup(engine)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	httpServer := &http.Server{Handler: engine, ReadHeaderTimeout: 5 * time.Second}
	done := make(chan error, 1)
	go func() { done <- httpServer.Serve(listener) }()
	if err := os.WriteFile(filepath.Join(filepath.Dir(configPath), "base"), []byte("http://"+listener.Addr().String()), 0600); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(8 * time.Minute)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(filepath.Join(filepath.Dir(configPath), "stop")); err == nil {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := httpServer.Shutdown(ctx); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err != http.ErrServerClosed {
		t.Fatal(err)
	}
	t.Log("owned Go HTTP listener and PostgreSQL/index clients closed")
}
