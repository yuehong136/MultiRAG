package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"multirag/internal/common"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/skills"
	"multirag/internal/storage"
	"multirag/internal/utility"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
	"go.uber.org/zap"

	"multirag/internal/cache"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/service"
	"multirag/internal/service/nlp"
	"multirag/internal/tokenizer"
)

func printHelp() {
	fmt.Fprintf(os.Stderr, "Usage: %s [OPTIONS]\n\n", os.Args[0])
	fmt.Fprintf(os.Stderr, "MultiRAG Server - Enterprise-grade RAG engine based on deep document understanding\n\n")
	fmt.Fprintf(os.Stderr, "Options:\n")
	fmt.Fprintf(os.Stderr, "  -p, --port int\tServer port (overrides config file)\n")
	fmt.Fprintf(os.Stderr, "  -h, --help   \tShow this help message and exit\n")
	fmt.Fprintf(os.Stderr, "\nExamples:\n")
	fmt.Fprintf(os.Stderr, "  %s           # Start server with config file port\n", os.Args[0])
	fmt.Fprintf(os.Stderr, "  %s -p 8080   # Start server on port 8080\n", os.Args[0])
	fmt.Fprintf(os.Stderr, "  %s --port 8080 # Start server on port 8080\n", os.Args[0])
}

func main() {
	// Parse command line flags
	var portFlag int
	flag.IntVar(&portFlag, "port", 0, "Server port (overrides config file)")
	flag.IntVar(&portFlag, "p", 0, "Server port (shorthand, overrides config file)")

	// Custom help message
	flag.Usage = printHelp

	flag.Parse()

	// Initialize logger with default level
	if err := common.Init("info"); err != nil {
		panic(fmt.Sprintf("Failed to initialize logger: %v", err))
	}

	defer common.Sync()
	server.SetLogger(common.Logger)

	// Initialize configuration
	if err := server.Init(""); err != nil {
		common.Fatal("Failed to initialize config", zap.Error(err))
	}

	// Override port with command line argument if provided
	config := server.GetConfig()
	if portFlag > 0 {
		config.Server.Port = portFlag
		common.Info("Port overridden by command line argument", zap.Int("port", portFlag))
	}

	if config.Server.Port == 0 {
		common.Fatal("Server port is not configured. Please specify via --port flag or config file.")
	}

	// Load model providers configuration
	if err := server.LoadModelProviders(""); err != nil {
		common.Fatal("Failed to load model providers", zap.Error(err))
	}
	common.Info("Model providers loaded", zap.Int("count", len(server.GetModelProviders())))

	// Update the shared level without replacing captured loggers
	if config.Log.Level != "" && config.Log.Level != "info" {
		if err := common.Init(config.Log.Level); err != nil {
			common.Error("Failed to apply configured log level", err)
		}
	}
	// Seed the log level from the active logger so `list configs` reports a value
	// even when it is omitted from the config file.
	if config.Log.Level == "" {
		config.Log.Level = common.GetLevel()
	}

	common.Info("Server mode", zap.String("mode", config.Server.Mode))

	// Print all configuration settings
	server.PrintAll()

	// Initialize database
	if err := dao.InitDB(); err != nil {
		common.Fatal("Failed to initialize database", zap.Error(err))
	}

	// Initialize LLM factory data models from configuration file
	if err := dao.InitLLMFactory(); err != nil {
		common.Error("Failed to initialize LLM factory", err)
	} else {
		common.Info("LLM factory initialized successfully")
	}

	// Initialize doc engine
	if err := engine.Init(&config.DocEngine); err != nil {
		common.Fatal("Failed to initialize doc engine", zap.Error(err))
	}
	defer engine.Close()

	// Initialize Redis cache
	if err := cache.Init(&config.Redis); err != nil {
		common.Fatal("Failed to initialize Redis", zap.Error(err))
	}
	defer cache.Close()

	if err := storage.InitStorageFactory(); err != nil {
		common.Fatal("Failed to initialize storage factory", zap.Error(err))
	}

	// Initialize server variables (runtime variables that can change during operation)
	// This must be done after Cache is initialized
	if err := server.InitVariables(cache.Get()); err != nil {
		common.Warn("Failed to initialize server variables from Redis, using defaults", zap.String("error", err.Error()))
	}

	// Initialize admin status (default: unavailable=1)
	local.InitAdminStatus(1, "admin server not connected")

	// Initialize tokenizer (rag_analyzer)
	// DictPath is auto-detected: RAG_DICT_PATH env > ./resource > /usr/share/infinity/resource
	tokenizerCfg := &tokenizer.PoolConfig{}
	if err := tokenizer.Init(tokenizerCfg); err != nil {
		common.Fatal("Failed to initialize tokenizer", zap.Error(err))
	}
	defer tokenizer.Close()

	// Initialize global QueryBuilder using tokenizer's DictPath
	if err := nlp.InitQueryBuilderFromTokenizer(tokenizerCfg.DictPath); err != nil {
		common.Fatal("Failed to initialize query builder", zap.Error(err))
	}

	startServer(config)

	common.Info("Server exited")
}

func startServer(config *server.Config) {

	// Set Gin mode
	if config.Server.Mode == "release" {
		gin.SetMode(gin.ReleaseMode)
	} else {
		gin.SetMode(gin.DebugMode)
	}

	// Initialize service layer
	userService := service.NewUserService()
	documentService := service.NewDocumentService()
	datasetsService := service.NewDatasetsService()
	kbService := service.NewKnowledgebaseService()
	chunkService := service.NewChunkService()
	llmService := service.NewLLMService()
	tenantService := service.NewTenantService()
	chatService := service.NewChatService()
	chatSessionService := service.NewChatSessionService()
	systemService := service.NewSystemService()
	connectorService := service.NewConnectorService()
	searchService := service.NewSearchService()
	fileService := service.NewFileService()
	memoryService := service.NewMemoryService()
	modelProviderService := service.NewModelProviderService()
	taskService := service.NewTaskService(dao.DB, cache.Get().GetClient())

	// Initialize handler layer
	authHandler := handler.NewAuthHandler()
	userHandler := handler.NewUserHandler(userService)
	tenantHandler := handler.NewTenantHandler(tenantService, userService)
	documentHandler := handler.NewDocumentHandler(documentService)
	datasetsHandler := handler.NewDatasetsHandler(datasetsService)
	systemHandler := handler.NewSystemHandler(systemService)
	kbHandler := handler.NewKnowledgebaseHandler(kbService, userService, documentService)
	chunkHandler := handler.NewChunkHandler(chunkService, userService)
	llmHandler := handler.NewLLMHandler(llmService, userService)
	chatHandler := handler.NewChatHandler(chatService, userService)
	chatSessionHandler := handler.NewChatSessionHandler(chatSessionService, userService)
	connectorHandler := handler.NewConnectorHandler(connectorService, userService)
	searchHandler := handler.NewSearchHandler(searchService, userService)
	fileHandler := handler.NewFileHandler(fileService, userService)
	memoryHandler := handler.NewMemoryHandler(memoryService)
	providerHandler := handler.NewProviderHandler(userService, modelProviderService)
	taskHandler := handler.NewTaskHandler(taskService)

	// Initialize router
	r := router.NewRouter(authHandler, userHandler, tenantHandler, documentHandler, datasetsHandler, systemHandler, kbHandler, chunkHandler, llmHandler, chatHandler, chatSessionHandler, connectorHandler, searchHandler, fileHandler, memoryHandler, providerHandler, taskHandler)

	// Create Gin engine
	ginEngine := gin.New()

	// Middleware
	if config.Server.Mode == "debug" {
		ginEngine.Use(gin.Logger())
	}
	ginEngine.Use(gin.Recovery())

	// Setup routes
	r.Setup(ginEngine)
	coreBlobs, blobErr := skills.NewObjectStore(config.StorageEngine.Minio)
	if blobErr != nil {
		coreBlobs = nil
	}
	var coreStore service.SkillBlobStore
	if coreBlobs != nil {
		coreStore = coreBlobs
	}
	stopCore, coreErr := handler.AttachSkillCore(ginEngine, config, skills.New(dao.DB, nil, nil, nil).Authenticate, func(ctx context.Context, tenant string) (any, error) {
		return (&skills.LegacyModels{DB: dao.DB, Providers: dao.GetModelProviderManager()}).List(ctx, tenant)
	}, coreStore)
	if coreErr != nil {
		panic(coreErr)
	}
	defer stopCore()
	stopSkills := skills.Attach(ginEngine, config)
	defer stopSkills()

	// Create HTTP server
	addr := fmt.Sprintf(":%d", config.Server.Port)
	srv := &http.Server{
		Addr:    addr,
		Handler: ginEngine,
	}

	// Start server in a goroutine
	go func() {
		common.Info(
			"\n     __  ___      ____  _ ____  ___   ______\n" +
				"    /  |/  /_  __/ / /_(_) __ \\/   | / ____/\n" +
				"   / /|_/ / / / / / __/ / /_/ / /| |/ / __  \n" +
				"  / /  / / /_/ / / /_/ / _, _/ ___ / /_/ /  \n" +
				" /_/  /_/\\__,_/_/\\__/_/_/ |_/_/  |_\\____/   \n",
		)
		common.Info(fmt.Sprintf("MultiRAG Go Version: %s", utility.GetMultiRAGVersion()))
		common.Info(fmt.Sprintf("Server starting on port: %d", config.Server.Port))
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			common.Fatal("Failed to start server", zap.Error(err))
		}
	}()

	// Get local IP address for heartbeat reporting
	localIP, err := utility.GetLocalIP()
	if err != nil {
		common.Warn("Unable to resolve heartbeat IPv4 address; using loopback", zap.Error(err))
		localIP = "127.0.0.1"
	}

	// Initialize and start heartbeat reporter to admin server
	heartbeatService := service.NewHeartbeatSender(
		common.Logger,
		common.ServerTypeAPI,
		fmt.Sprintf("multirag-server-%d", config.Server.Port),
		localIP,
		config.Server.Port,
	)
	if err := heartbeatService.InitHTTPClient(); err != nil {
		common.Warn("Failed to initialize heartbeat service", zap.Error(err))
	} else {
		heartbeatReporter := utility.NewScheduledTask("Heartbeat reporter", 3*time.Second, func() {
			if err = heartbeatService.SendHeartbeat(); err == nil {
				local.SetAdminStatus(0, "")
			} else {
				local.SetAdminStatus(1, err.Error())
			}
		})
		heartbeatReporter.Start()
		defer heartbeatReporter.Stop()
	}

	// Wait for interrupt signal to gracefully shutdown
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM, syscall.SIGQUIT, syscall.SIGUSR2)
	sig := <-quit

	common.Info(fmt.Sprintf("Receives %s signal to shutdown server", strings.ToUpper(sig.String())))
	common.Info("Shutting down server...")

	// Create context with timeout for graceful shutdown
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	// Shutdown server
	if err := srv.Shutdown(ctx); err != nil {
		common.Fatal("Server forced to shutdown", zap.Error(err))
	}
}
