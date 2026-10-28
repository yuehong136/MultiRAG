package handler_test

import (
	"context"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	gormlog "gorm.io/gorm/logger"
	"multirag/internal/admin"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
)

// Driven by shared_secret_live against an owned scratch PostgreSQL database.
func TestHybridAuthLiveServer(t *testing.T) {
	config := os.Getenv("MULTIRAG_HYBRID_LIVE_CONFIG")
	if config == "" {
		t.Skip("requires owned Python scratch harness")
	}
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal(err)
	}
	if err := server.InitVariables(nil); err != nil {
		t.Fatal(err)
	}
	dsn, err := dao.PostgresDSN(server.GetConfig().Database)
	if err != nil {
		t.Fatal(err)
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: gormlog.Default.LogMode(gormlog.Silent)})
	if err != nil {
		t.Fatal("scratch SQL connection failed")
	}
	dao.DB = db
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	local.InitAdminStatus(0, "owned acceptance")
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.Use(gin.Recovery())
	users := service.NewUserService()
	appRouter := router.NewRouter(handler.NewAuthHandler(), handler.NewUserHandler(users), handler.NewTenantHandler(service.NewTenantService(), users), nil, handler.NewDatasetsHandler(service.NewDatasetsService()), handler.NewSystemHandler(service.NewSystemService()), nil, nil, nil, handler.NewChatSessionHandler(service.NewChatSessionService(), users), nil, nil, nil, nil, nil, nil)
	appRouter.Setup(app)
	adminHandler := admin.NewHandler(admin.NewService())
	adminApp := gin.New()
	adminApp.Use(gin.Recovery())
	admin.NewRouter(adminHandler).Setup(adminApp)
	app.Any("/api/v1/admin/*path", gin.WrapH(adminApp))
	app.POST("/owned-admin/login", adminHandler.Login)
	// A real downstream SQL mutation makes an accidental non-Abort observable.
	app.POST("/owned-admin/mutate", adminHandler.AuthMiddleware(), func(c *gin.Context) {
		if err := db.Exec("UPDATE usr_ai.t_ai_users SET nickname = nickname || ' [admin probe]'").Error; err != nil {
			c.AbortWithStatus(500)
			return
		}
		c.JSON(200, gin.H{"code": 0})
	})
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	srv := &http.Server{Handler: app, ReadHeaderTimeout: 5 * time.Second}
	done := make(chan error, 1)
	go func() { done <- srv.Serve(listener) }()
	dir := filepath.Dir(config)
	if err := os.WriteFile(filepath.Join(dir, "go-base"), []byte("http://"+listener.Addr().String()), 0600); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Minute)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(filepath.Join(dir, "go-stop")); err == nil {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err != http.ErrServerClosed {
		t.Fatal(err)
	}
}
