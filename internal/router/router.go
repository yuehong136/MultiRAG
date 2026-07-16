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

	"multirag/internal/handler"
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
	llmHandler           *handler.LLMHandler
	chatHandler          *handler.ChatHandler
	chatSessionHandler   *handler.ChatSessionHandler
	connectorHandler     *handler.ConnectorHandler
	searchHandler        *handler.SearchHandler
	fileHandler          *handler.FileHandler
	memoryHandler        *handler.MemoryHandler
	providerHandler      *handler.ProviderHandler
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
	llmHandler *handler.LLMHandler,
	chatHandler *handler.ChatHandler,
	chatSessionHandler *handler.ChatSessionHandler,
	connectorHandler *handler.ConnectorHandler,
	searchHandler *handler.SearchHandler,
	fileHandler *handler.FileHandler,
	memoryHandler *handler.MemoryHandler,
	providerHandler *handler.ProviderHandler,
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
		llmHandler:           llmHandler,
		chatHandler:          chatHandler,
		chatSessionHandler:   chatSessionHandler,
		connectorHandler:     connectorHandler,
		searchHandler:        searchHandler,
		fileHandler:          fileHandler,
		memoryHandler:        memoryHandler,
		providerHandler:      providerHandler,
	}
}

// Setup setup routes
func (r *Router) Setup(engine *gin.Engine) {
	// Health check
	engine.GET("/health", r.systemHandler.Health)

	// System endpoints
	engine.GET("/v1/system/ping", r.systemHandler.Ping)
	engine.GET("/api/v1/system/ping", r.systemHandler.Ping)
	engine.GET("/v1/system/config", r.systemHandler.GetConfig)
	engine.GET("/v1/system/configs", r.systemHandler.GetConfigs)
	engine.GET("/v1/system/version", r.systemHandler.GetVersion)
	engine.POST("/v1/user/register", r.userHandler.Register)
	// User login channels endpoint
	engine.GET("/v1/user/login/channels", r.userHandler.GetLoginChannels)

	// User login by email endpoint
	engine.POST("/v1/user/login", r.userHandler.LoginByEmail)

	// User logout endpoint
	engine.GET("/v1/user/logout", r.userHandler.Logout)

	// Protected routes
	authorized := engine.Group("")
	authorized.Use(r.authHandler.AuthMiddleware())
	{
		// User info endpoint
		authorized.GET("/v1/user/info", r.userHandler.Info)
		// User tenant info endpoint
		authorized.GET("/v1/user/tenant_info", r.tenantHandler.TenantInfo)
		// Tenant list endpoint
		authorized.GET("/v1/tenant/list", r.tenantHandler.TenantList)
		// Tenant doc engine metadata table endpoints (per-tenant resources)
		authorized.POST("/v1/tenant/doc_engine_metadata_table", r.tenantHandler.CreateMetadataInDocEngine)   // Internal API only for GO
		authorized.DELETE("/v1/tenant/doc_engine_metadata_table", r.tenantHandler.DeleteMetadataInDocEngine) // Internal API only for GO
		// Tenant metadata insert from file (internal)
		authorized.POST("/v1/tenant/insert_metadata_from_file", r.tenantHandler.InsertMetadataFromFile) // Internal API only for GO
		// User settings endpoint
		authorized.POST("/v1/user/setting", r.userHandler.Setting)
		// User change password endpoint
		authorized.POST("/v1/user/setting/password", r.userHandler.ChangePassword)
		// User set tenant info endpoint
		authorized.POST("/v1/user/set_tenant_info", r.userHandler.SetTenantInfo)

		// API v1 route group
		v1 := authorized.Group("/api/v1")
		{
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
				documents.DELETE("/:id", r.documentHandler.DeleteDocument)
			}

			// RESTful dataset routes
			datasets := v1.Group("/datasets")
			{
				datasets.GET("", r.datasetsHandler.ListDatasets)
				datasets.POST("", r.datasetsHandler.CreateDataset)
				datasets.DELETE("", r.datasetsHandler.DeleteDatasets)
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
				provider.POST("/", r.providerHandler.AddProvider)
				provider.GET("/:provider_name", r.providerHandler.ShowProvider)
				provider.DELETE("/:provider_name", r.providerHandler.DeleteProvider)
				provider.GET("/:provider_name/models", r.providerHandler.ListModels)
				provider.GET("/:provider_name/models/:model_name", r.providerHandler.ShowModel)
				provider.POST("/:provider_name/instances", r.providerHandler.CreateProviderInstance)
				provider.GET("/:provider_name/instances", r.providerHandler.ListProviderInstances)
				provider.GET("/:provider_name/instances/:instance_name", r.providerHandler.ShowProviderInstance)
				provider.PUT("/:provider_name/instances/:instance_name", r.providerHandler.AlterProviderInstance)
				provider.DELETE("/:provider_name/instances/:instance_name", r.providerHandler.DropProviderInstance)
				provider.GET("/:provider_name/instances/:instance_name/models", r.providerHandler.ListInstanceModels)
				provider.PUT("/:provider_name/instances/:instance_name/models/:model_name", r.providerHandler.EnableOrDisableModel)
				provider.POST("/:provider_name/instances/:instance_name/models/:model_name", r.providerHandler.ChatToModel)
			}

			// File routes (RESTful, aligned with Python /api/v1/files)
			files := v1.Group("/files")
			{
				files.POST("", r.fileHandler.UploadFile)
				files.GET("", r.fileHandler.ListFiles)
				files.DELETE("", r.fileHandler.DeleteFiles)
				files.POST("/move", r.fileHandler.MoveFiles)
				files.GET("/:id/ancestors", r.fileHandler.GetFileAncestors)
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
				system.GET("/version", r.systemHandler.GetVersion)
				system.GET("/configs", r.systemHandler.GetConfigs)
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
			kb.POST("/doc_engine_table", r.knowledgebaseHandler.CreateDatasetInDocEngine)   // Internal API only for GO
			kb.DELETE("/doc_engine_table", r.knowledgebaseHandler.DeleteDatasetInDocEngine) // Internal API only for GO
			kb.POST("/insert_from_file", r.knowledgebaseHandler.InsertDatasetFromFile)      // Internal API only for GO

			// KB ID specific routes
			kbByID := kb.Group("/:kb_id")
			{
				kbByID.GET("/tags", r.knowledgebaseHandler.ListTags)
				kbByID.POST("/rm_tags", r.knowledgebaseHandler.RemoveTags)
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
			doc.POST("/set_meta", r.documentHandler.SetMeta)
		}

		// Chunk routes
		chunk := authorized.Group("/v1/chunk")
		{
			chunk.POST("/retrieval_test", r.chunkHandler.RetrievalTest)
			chunk.GET("/get", r.chunkHandler.Get)
			chunk.POST("/list", r.chunkHandler.List)
			chunk.POST("/update", r.chunkHandler.UpdateChunk) // Internal API only for GO
			chunk.POST("/rm", r.chunkHandler.Remove)
		}

		// LLM routes
		llm := authorized.Group("/v1/llm")
		{
			llm.GET("/my_llms", r.llmHandler.GetMyLLMs)
			llm.GET("/factories", r.llmHandler.Factories)
			llm.GET("/list", r.llmHandler.ListApp)
			llm.POST("/set_api_key", r.llmHandler.SetAPIKey)
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
