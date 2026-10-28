//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

package router

import (
	"github.com/gin-gonic/gin"
	"strings"

	"multirag/internal/handler"
	"multirag/internal/service"
)

// Router router
type Router struct {
	authHandler          *handler.AuthHandler
	userHandler          *handler.UserHandler
	tenantHandler        *handler.TenantHandler
	documentHandler      *handler.DocumentHandler
	datasetsHandler      *handler.DatasetsHandler
	systemHandler        *handler.SystemHandler
	knowledgebaseHandler *handler.KnowledgebaseHandler
	chunkHandler         *handler.ChunkHandler
	chatHandler          *handler.ChatHandler
	chatSessionHandler   *handler.ChatSessionHandler
	connectorHandler     *handler.ConnectorHandler
	mcpHandler           *handler.MCPHandler
	searchHandler        *handler.SearchHandler
	fileHandler          *handler.FileHandler
	memoryHandler        *handler.MemoryHandler
	providerHandler      *handler.ProviderHandler
	taskHandler          *handler.TaskHandler
	agentHandler         *handler.AgentHandler
	searchbotHandler     *handler.SearchbotHandler
}

// NewRouter create router
func NewRouter(
	authHandler *handler.AuthHandler,
	userHandler *handler.UserHandler,
	tenantHandler *handler.TenantHandler,
	documentHandler *handler.DocumentHandler,
	datasetsHandler *handler.DatasetsHandler,
	systemHandler *handler.SystemHandler,
	knowledgebaseHandler *handler.KnowledgebaseHandler,
	chunkHandler *handler.ChunkHandler,
	chatHandler *handler.ChatHandler,
	chatSessionHandler *handler.ChatSessionHandler,
	connectorHandler *handler.ConnectorHandler,
	searchHandler *handler.SearchHandler,
	fileHandler *handler.FileHandler,
	memoryHandler *handler.MemoryHandler,
	providerHandler *handler.ProviderHandler,
	taskHandler *handler.TaskHandler,
) *Router {
	return &Router{
		authHandler:          authHandler,
		userHandler:          userHandler,
		tenantHandler:        tenantHandler,
		documentHandler:      documentHandler,
		datasetsHandler:      datasetsHandler,
		systemHandler:        systemHandler,
		knowledgebaseHandler: knowledgebaseHandler,
		chunkHandler:         chunkHandler,
		chatHandler:          chatHandler,
		chatSessionHandler:   chatSessionHandler,
		connectorHandler:     connectorHandler,
		searchHandler:        searchHandler,
		fileHandler:          fileHandler,
		memoryHandler:        memoryHandler,
		providerHandler:      providerHandler,
		taskHandler:          taskHandler,
	}
}

// SetSearchbotHandler binds the model service shared by the server's handlers.
func (r *Router) SetSearchbotHandler(searchbotHandler *handler.SearchbotHandler) {
	r.searchbotHandler = searchbotHandler
}

// Setup setup routes
func (r *Router) Setup(engine *gin.Engine) {
	// Mark even authentication denials before the protected middleware runs.
	engine.Use(func(c *gin.Context) {
		if c.Request.URL.Path == "/api/v1/thumbnails" || strings.HasPrefix(c.Request.URL.Path, "/api/v1/documents/images/") {
			c.Header("Cache-Control", "no-store")
			c.Header("X-Content-Type-Options", "nosniff")
		}
		c.Next()
	})
	if r.mcpHandler == nil {
		r.mcpHandler = handler.NewMCPHandler(service.NewMCPService())
	}
	if r.agentHandler == nil {
		r.agentHandler = handler.NewAgentHandler(service.NewAgentService())
	}
	if r.searchbotHandler == nil {
		r.searchbotHandler = handler.NewSearchbotHandler(service.NewSearchbotService(service.NewModelProviderService()))
	}
	// Health check
	engine.GET("/health", r.systemHandler.Health)

	engine.POST("/api/v1/datasets/:dataset_id/documents/batch-update-status", r.authHandler.DocumentStatusAuthMiddleware(), r.documentHandler.BatchUpdateStatus)

	public := engine.Group("/api/v1")
	{
		public.GET("/system/ping", r.systemHandler.Ping)
		public.GET("/system/healthz", r.systemHandler.Healthz)
		public.GET("/system/config", r.systemHandler.GetConfig)
		public.GET("/system/version", r.systemHandler.GetVersion)
		public.POST("/auth/login", r.userHandler.Login)
		public.POST("/users", r.userHandler.Register)
		public.GET("/auth/login/channels", r.userHandler.GetLoginChannels)
		public.GET("/auth/login/:channel", r.userHandler.OAuthLogin)
		public.GET("/auth/oauth/:channel/callback", r.userHandler.OAuthCallback)
		public.GET("/connectors/google-drive/oauth/web/callback", r.connectorHandler.GoogleDriveWebOAuthCallback)
		public.GET("/connectors/gmail/oauth/web/callback", r.connectorHandler.GmailWebOAuthCallback)
	}
	// Protected routes
	authorized := engine.Group("")
	authorized.Use(r.authHandler.AuthMiddleware())
	{
		authorized.GET("/api/v1/mcp/servers", r.mcpHandler.ListMCPServers)
		authorized.POST("/api/v1/mcp/servers", r.mcpHandler.CreateMCPServer)
		authorized.PUT("/api/v1/mcp/servers/:mcp_id", r.mcpHandler.UpdateMCPServer)
		authorized.DELETE("/api/v1/mcp/servers/:mcp_id", r.mcpHandler.DeleteMCPServer)
		authorized.POST("/api/v1/auth/logout", r.userHandler.Logout)
		authorized.GET("/api/v1/users/me", r.userHandler.Info)
		authorized.PATCH("/api/v1/users/me", r.userHandler.Setting)
		authorized.GET("/api/v1/users/me/models", r.tenantHandler.TenantInfo)
		authorized.PATCH("/api/v1/users/me/models", r.userHandler.SetTenantInfo)
		authorized.GET("/api/v1/tenants", r.tenantHandler.TenantList)
		authorized.PATCH("/api/v1/tenants/:tenant_id", r.tenantHandler.AcceptTenantInvite)
		authorized.GET("/api/v1/tenants/:tenant_id/users", r.tenantHandler.ListTenantMembers)
		authorized.POST("/api/v1/tenants/:tenant_id/users", r.tenantHandler.AddTenantMember)
		authorized.DELETE("/api/v1/tenants/:tenant_id/users", r.tenantHandler.RemoveTenantMember)
		authorized.GET("/api/v1/tenant/list", r.tenantHandler.TenantList)
		authorized.POST("/api/v1/connectors/google/oauth/web/start", r.connectorHandler.StartGoogleWebOAuth)
		authorized.POST("/api/v1/connectors/google/oauth/web/result", r.connectorHandler.PollGoogleWebOAuthResult)
		authorized.GET("/api/v1/connectors/", r.connectorHandler.ListConnectors)
		authorized.POST("/api/v1/connectors/", r.connectorHandler.CreateConnector)
		authorized.PATCH("/api/v1/connectors/:connector_id", r.connectorHandler.UpdateConnector)
		authorized.GET("/api/v1/connectors/:connector_id", r.connectorHandler.GetConnector)
		authorized.GET("/api/v1/connectors/:connector_id/logs", r.connectorHandler.ListLogs)
		authorized.DELETE("/api/v1/connectors/:connector_id", r.connectorHandler.DeleteConnector)
		authorized.POST("/api/v1/connectors/:connector_id/rebuild", r.connectorHandler.RebuildConnector)
		authorized.GET("/api/v1/chats/:chat_id/sessions", r.chatSessionHandler.ListChatSessions)
		// User info endpoint
		authorized.GET("/v1/user/info", r.userHandler.Info)
		// User tenant info endpoint
		authorized.GET("/v1/user/tenant_info", r.tenantHandler.TenantInfo)
		// Tenant list endpoint
		authorized.GET("/v1/tenant/list", r.tenantHandler.TenantList)
		// Tenant doc engine metadata table endpoints (per-tenant resources)
		authorized.POST("/api/v1/tenant/metadata_store", r.tenantHandler.CreateMetadataStore)   // Internal API only for GO
		authorized.DELETE("/api/v1/tenant/metadata_store", r.tenantHandler.DeleteMetadataStore) // Internal API only for GO
		// Tenant metadata insert from file (internal)
		authorized.POST("/api/v1/tenant/insert_metadata_from_file", r.tenantHandler.InsertMetadataFromFile) // Internal API only for GO
		// User settings endpoint
		authorized.POST("/v1/user/setting", r.userHandler.Setting)
		// User change password endpoint
		authorized.POST("/v1/user/setting/password", r.userHandler.ChangePassword)
		// User set tenant info endpoint
		authorized.POST("/v1/user/set_tenant_info", r.userHandler.SetTenantInfo)

		// API v1 route group
		v1 := authorized.Group("/api/v1")
		{
			v1.POST("/tasks/:task_id/cancel", r.taskHandler.Cancel)
			v1.PATCH("/tasks/:task_id", r.taskHandler.Update)
			// User routes
			//users := v1.Group("/users")
			//{
			//	users.POST("/register", r.userHandler.Register)
			//	users.POST("/login", r.userHandler.Login)
			//	users.GET("", r.userHandler.ListUsers)
			//	users.GET("/:id", r.userHandler.GetUserByID)
			//}

			// Document routes
			documents := v1.Group("/documents")
			{
				documents.POST("", r.documentHandler.CreateDocument)
				documents.GET("", r.documentHandler.ListDocuments)
				documents.GET("/:id", r.documentHandler.GetDocumentByID)
				documents.PUT("/:id", r.documentHandler.UpdateDocument)
			}

			v1.GET("/thumbnails", r.documentHandler.GetThumbnails)
			v1.GET("/documents/images/*image_id", r.documentHandler.GetDocumentImage)

			// RESTful dataset routes
			datasets := v1.Group("/datasets")
			{
				datasets.DELETE("/:dataset_id/documents", r.documentHandler.DeleteDocuments)
				datasets.POST("/:dataset_id/documents/stop", r.documentHandler.StopParseDocuments)
				datasets.GET("", r.datasetsHandler.ListDatasets)
				datasets.GET("/:dataset_id", r.datasetsHandler.GetDataset)
				datasets.GET("/:dataset_id/ingestions/summary", r.datasetsHandler.GetIngestionSummary)
				datasets.GET("/:dataset_id/metadata/config", r.datasetsHandler.GetMetadataConfig)
				datasets.PUT("/:dataset_id/metadata/config", r.datasetsHandler.UpdateMetadataConfig)
				datasets.GET("/:dataset_id/ingestions", r.datasetsHandler.ListIngestionLogs)
				datasets.GET("/:dataset_id/ingestions/:log_id", r.datasetsHandler.GetIngestionLog)
				datasets.GET("/:dataset_id/graph", r.datasetsHandler.GetKnowledgeGraph)
				datasets.DELETE("/:dataset_id/graph", r.datasetsHandler.DeleteKnowledgeGraph)
				datasets.POST("", r.datasetsHandler.CreateDataset)
				datasets.DELETE("", r.datasetsHandler.DeleteDatasets)
				datasets.POST("/search", r.chunkHandler.SearchDatasets)
				datasets.GET("/:dataset_id/documents", r.documentHandler.ListDatasetDocuments)
				datasets.GET("/:dataset_id/documents/:document_id/chunks/:chunk_id", r.chunkHandler.Get)
				datasets.GET("/metadata/flattened", r.datasetsHandler.ListMetadataFlattened)
				datasets.DELETE("/:dataset_id/tags", r.datasetsHandler.RemoveTags)
			}

			// Author routes
			authors := v1.Group("/authors")
			{
				authors.GET("/:author_id/documents", r.documentHandler.GetDocumentsByAuthorID)
			}

			// Memory routes
			memory := v1.Group("/memories")
			{
				memory.POST("", r.memoryHandler.CreateMemory)
				memory.PUT("/:memory_id", r.memoryHandler.UpdateMemory)
				memory.DELETE("/:memory_id", r.memoryHandler.DeleteMemory)
				memory.GET("", r.memoryHandler.ListMemories)
				memory.GET("/:memory_id/config", r.memoryHandler.GetMemoryConfig)
				memory.GET("/:memory_id", r.memoryHandler.GetMemoryMessages)
			}

			// provider pool route group
			provider := v1.Group("/providers")
			{
				provider.GET("/", r.providerHandler.ListProviders)
				provider.PUT("/", r.providerHandler.AddProvider)
				provider.GET("/:provider_name", r.providerHandler.ShowProvider)
				provider.DELETE("/:provider_name", r.providerHandler.DeleteProvider)
				provider.GET("/:provider_name/models", r.providerHandler.ListModels)
				provider.GET("/:provider_name/models/:model_name", r.providerHandler.ShowModel)
				provider.POST("/:provider_name/instances", r.providerHandler.CreateProviderInstance)
				provider.GET("/:provider_name/instances", r.providerHandler.ListProviderInstances)
				provider.GET("/:provider_name/instances/:instance_name", r.providerHandler.ShowProviderInstance)
				provider.GET("/:provider_name/instances/:instance_name/balance", r.providerHandler.ShowInstanceBalance)
				provider.GET("/:provider_name/connection", r.providerHandler.CheckConnection)
				provider.GET("/:provider_name/instances/:instance_name/connection", r.providerHandler.CheckProviderConnection)
				provider.GET("/:provider_name/instances/:instance_name/tasks", r.providerHandler.ListTasks)
				provider.GET("/:provider_name/instances/:instance_name/tasks/:task_id", r.providerHandler.ShowTask)
				provider.PUT("/:provider_name/instances/:instance_name", r.providerHandler.AlterProviderInstance)
				provider.DELETE("/:provider_name/instances", r.providerHandler.DropProviderInstance)
				provider.GET("/:provider_name/instances/:instance_name/models", r.providerHandler.ListInstanceModels)
				provider.PATCH("/:provider_name/instances/:instance_name/models/*model_name", r.providerHandler.EnableOrDisableModel)
				provider.POST("/:provider_name/instances/:instance_name/models", r.providerHandler.AddCustomModel)
				provider.DELETE("/:provider_name/instances/:instance_name/models", r.providerHandler.DropInstanceModels)
				v1.POST("/chat/completions", r.providerHandler.ChatToModel)
				v1.POST("/embeddings", r.providerHandler.EmbedText)
				v1.POST("/rerank", r.providerHandler.RerankDocument)
				v1.POST("/audio/transcriptions", r.providerHandler.TranscribeAudio)
				v1.POST("/audio/speech", r.providerHandler.AudioSpeech)
				v1.POST("/file/ocr", r.providerHandler.OCRFile)
				v1.POST("/file/parse", r.providerHandler.ParseFile)
			}

			v1.GET("/agents", r.agentHandler.ListAgents)
			v1.GET("/agents/:agent_id/versions", r.agentHandler.ListAgentVersions)
			v1.GET("/agents/:agent_id/versions/:version_id", r.agentHandler.GetAgentVersion)
			v1.POST("/agents/:agent_id/upload", r.agentHandler.UploadAgentFile)
			v1.POST("/searchbots/related_questions", r.searchbotHandler.Handle)

			models := v1.Group("/models")
			{
				models.GET("", r.tenantHandler.GetModels)
				models.PATCH("", r.tenantHandler.SetModels)
			}

			// File routes (RESTful, aligned with Python /api/v1/files)
			files := v1.Group("/files")
			{
				files.POST("", r.fileHandler.UploadFile)
				files.GET("", r.fileHandler.ListFiles)
				files.DELETE("", r.fileHandler.DeleteFiles)
				files.POST("/move", r.fileHandler.MoveFiles)
				files.GET("/:id/ancestors", r.fileHandler.GetFileAncestors)
				files.GET("/:id/parent", r.fileHandler.GetParentFolder)
				files.GET("/:id", r.fileHandler.Download)
			}

			// Chat routes (RESTful, aligned with Python /api/v1/chats)
			chats := v1.Group("/chats")
			{
				chats.GET("", r.chatHandler.ListChats)
				chats.GET("/:chat_id", r.chatHandler.GetChat)
			}

			// Search routes (RESTful, aligned with Python /api/v1/searches)
			searches := v1.Group("/searches")
			{
				searches.GET("", r.searchHandler.ListSearches)
				searches.POST("", r.searchHandler.CreateSearch)
				searches.GET("/:search_id", r.searchHandler.GetSearch)
				searches.PUT("/:search_id", r.searchHandler.UpdateSearch)
				searches.DELETE("/:search_id", r.searchHandler.DeleteSearch)
			}

			// System routes (RESTful, aligned with Python /api/v1/system)
			system := v1.Group("/system")
			{
				system.GET("/configs", r.systemHandler.GetConfigs)
				system.GET("/status", r.systemHandler.GetStatus)
				system.GET("/stats", r.systemHandler.GetStats)
				system.GET("/tokens", r.systemHandler.ListTokens)
				system.POST("/tokens", r.systemHandler.CreateToken)
				system.DELETE("/tokens/:token", r.systemHandler.DeleteToken)

				// Runtime log level: GET reads, PUT updates
				log := system.Group("/log")
				{
					log.GET("", r.systemHandler.GetLogLevel)
					log.PUT("", r.systemHandler.SetLogLevel)
				}
			}

			// TODO: Message routes - Implementation pending - depends on CanvasService, TaskService and embedding engine
			// message := v1.Group("/messages")
			// {
			// 	message.POST("", r.memoryHandler.AddMessage)
			// 	message.DELETE("/:memory_id/:message_id", r.memoryHandler.ForgetMessage)
			// 	message.PUT("/:memory_id/:message_id", r.memoryHandler.UpdateMessage)
			// 	message.GET("/search", r.memoryHandler.SearchMessage)
			// 	message.GET("", r.memoryHandler.GetMessages)
			// 	message.GET("/:memory_id/:message_id/content", r.memoryHandler.GetMessageContent)
			// }
		}

		// Knowledge base routes
		kb := authorized.Group("/v1/kb")
		{
			kb.POST("/update", r.knowledgebaseHandler.UpdateKB)
			kb.POST("/update_metadata_setting", r.knowledgebaseHandler.UpdateMetadataSetting)
			kb.GET("/detail", r.knowledgebaseHandler.GetDetail)
			kb.GET("/tags", r.knowledgebaseHandler.ListTagsFromKbs)
			kb.GET("/get_meta", r.knowledgebaseHandler.GetMeta)
			kb.GET("/basic_info", r.knowledgebaseHandler.GetBasicInfo)
			authorized.POST("/api/v1/tenant/chunk_store", r.knowledgebaseHandler.CreateChunkStore)                 // Internal API only for GO
			authorized.DELETE("/api/v1/tenant/chunk_store", r.knowledgebaseHandler.DeleteChunkStore)               // Internal API only for GO
			authorized.POST("/api/v1/tenant/insert_chunks_from_file", r.knowledgebaseHandler.InsertChunksFromFile) // Internal API only for GO

			// KB ID specific routes
			kbByID := kb.Group("/:kb_id")
			{
				kbByID.GET("/tags", r.knowledgebaseHandler.ListTags)
				kbByID.POST("/rename_tag", r.knowledgebaseHandler.RenameTag)
				kbByID.GET("/knowledge_graph", r.knowledgebaseHandler.KnowledgeGraph)
				kbByID.DELETE("/knowledge_graph", r.knowledgebaseHandler.DeleteKnowledgeGraph)
			}
		}

		// Document routes (metadata-oriented, web-style)
		doc := authorized.Group("/v1/document")
		{
			doc.POST("/list", r.documentHandler.ListDocumentsByKB)
			doc.POST("/metadata/summary", r.documentHandler.MetadataSummary)
			authorized.POST("/api/v1/document/set_meta", r.documentHandler.SetMeta)
			authorized.POST("/api/v1/document/delete_meta", r.documentHandler.DeleteMeta)
		}

		// Chunk routes
		chunk := authorized.Group("/v1/chunk")
		{
			chunk.POST("/list", r.chunkHandler.List)
			authorized.POST("/api/v1/chunk/update", r.chunkHandler.UpdateChunk) // Internal API only for GO
			authorized.DELETE("/api/v1/datasets/:dataset_id/documents/:document_id/chunks", r.chunkHandler.RemoveChunks)
		}

		// Chat routes
		chat := authorized.Group("/v1/dialog")
		{
			chat.POST("/set", r.chatHandler.SetDialog)
			chat.POST("/rm", r.chatHandler.RemoveChats)
		}

		// Chat session (conversation) routes
		session := authorized.Group("/v1/conversation")
		{
			session.POST("/set", r.chatSessionHandler.SetChatSession)
			session.POST("/rm", r.chatSessionHandler.RemoveChatSessions)
			session.GET("/list", r.chatSessionHandler.ListChatSessions)
			session.POST("/completion", r.chatSessionHandler.Completion)
		}

		// Connector routes
		connector := authorized.Group("/v1/connector")
		{
			connector.GET("/list", r.connectorHandler.ListConnectors)
		}

		// Search routes (legacy; list migrated to /api/v1/searches)
		search := authorized.Group("/v1/search")
		{
			search.POST("/list", r.searchHandler.ListSearches)
		}

		// File routes (legacy helpers; list/upload migrated to /api/v1/files)
		file := authorized.Group("/v1/file")
		{
			file.GET("/root_folder", r.fileHandler.GetRootFolder)
			file.GET("/parent_folder", r.fileHandler.GetParentFolder)
			file.GET("/all_parent_folder", r.fileHandler.GetAllParentFolders)
		}
	}

	// API routes listing
	engine.GET("/docs", func(c *gin.Context) {
		routes := engine.Routes()
		type routeInfo struct {
			Method string `json:"method"`
			Path   string `json:"path"`
		}
		var list []routeInfo
		for _, r := range routes {
			list = append(list, routeInfo{Method: r.Method, Path: r.Path})
		}
		c.JSON(200, gin.H{
			"title":  "MultiRAG Go API",
			"routes": list,
			"total":  len(list),
		})
	})

	// Handle undefined routes
	engine.NoRoute(handler.HandleNoRoute)
}
