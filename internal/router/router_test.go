package router

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"github.com/google/uuid"
	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	"gorm.io/gorm"
	"io"
	"multirag/internal/agent/runtime"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine"
	redisengine "multirag/internal/engine/redis"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/service/chunk"
	"multirag/internal/service/nlp"
	"multirag/internal/storage"
	"multirag/internal/testsupport"
	"multirag/internal/testsupport/scratch"
	"multirag/internal/utility"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
)

func TestConnectorRoutesDoNotConflictWithOAuthCallbacks(t *testing.T) {
	gin.SetMode(gin.TestMode)

	engine := gin.New()
	engine.GET("/api/v1/connectors/gmail/oauth/web/callback", func(c *gin.Context) {
		c.String(http.StatusOK, "gmail")
	})
	engine.GET("/api/v1/connectors/google-drive/oauth/web/callback", func(c *gin.Context) {
		c.String(http.StatusOK, "google-drive")
	})

	connectors := engine.Group("/api/v1/connectors")
	connectors.GET("/:connector_id", func(c *gin.Context) {
		c.String(http.StatusOK, c.Param("connector_id"))
	})
	connectors.GET("/:connector_id/logs", func(c *gin.Context) {
		c.String(http.StatusOK, c.Param("connector_id"))
	})

	tests := []struct {
		name string
		path string
		want string
	}{
		{
			name: "gmail callback",
			path: "/api/v1/connectors/gmail/oauth/web/callback",
			want: "gmail",
		},
		{
			name: "google drive callback",
			path: "/api/v1/connectors/google-drive/oauth/web/callback",
			want: "google-drive",
		},
		{
			name: "connector detail",
			path: "/api/v1/connectors/connector-1",
			want: "connector-1",
		},
		{
			name: "connector logs",
			path: "/api/v1/connectors/connector-1/logs",
			want: "connector-1",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			resp := httptest.NewRecorder()
			req := httptest.NewRequest(http.MethodGet, tt.path, nil)
			engine.ServeHTTP(resp, req)

			if resp.Code != http.StatusOK {
				t.Fatalf("status=%d body=%s", resp.Code, resp.Body.String())
			}
			if resp.Body.String() != tt.want {
				t.Fatalf("body=%q want=%q", resp.Body.String(), tt.want)
			}
		})
	}
}

func canonicalRouteEngine(selector *runtime.Selector) *gin.Engine {
	userService := service.NewUserService()
	documentService := service.NewDocumentService()
	datasetsService := service.NewDatasetService()
	knowledgebaseService := service.NewKnowledgebaseService()
	metadataService := service.NewMetadataService()
	chunkService := chunk.NewChunkService()
	llmService := service.NewLLMService()
	tenantService := service.NewTenantService()
	chatService := service.NewChatService()
	chatSessionService := service.NewChatSessionService()
	systemService := service.NewSystemService()
	connectorService := service.NewConnectorService()
	searchService := service.NewSearchService()
	fileService := service.NewFileService()
	memoryService := service.NewMemoryService()
	mcpService := service.NewMCPService()
	modelProviderService := service.NewModelProviderService()

	// Initialize doc engine for skill search
	docEngine := engine.Get()

	// Initialize handler layer
	authHandler := handler.NewAuthHandler()
	userHandler := handler.NewUserHandler(userService)
	tenantHandler := handler.NewTenantHandler(tenantService, userService, knowledgebaseService)
	documentHandler := handler.NewDocumentHandler(documentService, datasetsService)
	datasetsHandler := handler.NewDatasetsHandler(datasetsService, metadataService)
	systemHandler := handler.NewSystemHandler(systemService)
	knowledgebaseHandler := handler.NewKnowledgebaseHandler(knowledgebaseService, userService, documentService)
	chunkHandler := handler.NewChunkHandler(chunkService, userService)
	llmHandler := handler.NewLLMHandler(llmService, userService)
	chatHandler := handler.NewChatHandler(chatService, userService)
	chatSessionHandler := handler.NewChatSessionHandler(chatSessionService, userService)
	connectorHandler := handler.NewConnectorHandler(connectorService, userService)
	searchHandler := handler.NewSearchHandler(searchService, userService)
	fileHandler := handler.NewFileHandler(fileService, userService)
	memoryHandler := handler.NewMemoryHandler(memoryService)
	mcpHandler := handler.NewMCPHandler(mcpService)
	skillSearchHandler := handler.NewSkillSearchHandler(docEngine)
	providerHandler := handler.NewProviderHandler(userService, modelProviderService)
	agentHandler := handler.NewAgentHandler(service.NewAgentService(), fileService)
	searchBotLLM := &handler.SearchBotRealLLM{Svc: modelProviderService}
	searchBotHandler := handler.NewSearchBotHandler(
		searchService,
		tenantService,
		searchBotLLM,
		chunkService,
	)
	searchBotHandler.SetStreamLLM(searchBotLLM)
	searchBotHandler.SetAskService(service.NewAskService(chunkService, nil, 0, 0))
	pluginHandler := handler.NewPluginHandler(service.NewPluginService())
	modelHandler := handler.NewModelHandler(service.NewModelProviderService())
	fileCommitHandler := handler.NewFileCommitHandler(service.NewFileCommitService())

	// Dify retrieval handler
	docDAO := dao.NewDocumentDAO()
	retrievalService := nlp.NewRetrievalService(docEngine, docDAO)
	difyRetrievalHandler := handler.NewDifyRetrievalHandler(
		knowledgebaseService,
		modelProviderService,
		metadataService,
		retrievalService,
		docDAO,
		docEngine,
	)

	adminRuntimeHandler := handler.NewAdminRuntimeHandler(selector)
	r := NewRouter(authHandler, userHandler, tenantHandler, documentHandler, datasetsHandler, systemHandler, knowledgebaseHandler, chunkHandler, llmHandler, chatHandler, chatSessionHandler, connectorHandler, searchHandler, fileHandler, memoryHandler, mcpHandler, skillSearchHandler, providerHandler, agentHandler, searchBotHandler, difyRetrievalHandler, pluginHandler, modelHandler, fileCommitHandler, adminRuntimeHandler)
	app := gin.New()
	app.Use(gin.Recovery())
	r.Setup(app)
	return app
}

func TestCanonicalRouteTableAndAnonymousBoundaries(t *testing.T) {
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error", ""); err != nil {
		t.Fatal(err)
	}
	if err := server.Init(filepath.Join(filepath.Dir(os.Getenv("MULTIRAG_GO_ALIGNMENT_CONFIG")), "service_conf.yaml")); err != nil {
		t.Fatal("private router configuration", err)
	}
	prior := gin.Mode()
	gin.SetMode(gin.TestMode)
	t.Cleanup(func() { gin.SetMode(prior) })
	app := canonicalRouteEngine(nil)
	routes := map[string]string{}
	for _, route := range app.Routes() {
		routes[route.Method+" "+route.Path] = route.Handler
	}
	expected := map[string]string{
		"POST /api/v1/auth/login": ".LoginByEmail-fm",
		"POST /api/v1/users":      ".Register-fm",
		"POST /api/v1/providers/:provider_name/instances/:instance_name/models": ".AddModel-fm",
		"POST /api/v1/chat/completions":                                         ".ChatToModel-fm",
		"GET /api/v1/models/":                                                   ".ListTenantAddedModels-fm",
		"PATCH /api/v1/models/":                                                 ".SetModels-fm",
		"GET /api/v1/models/default":                                            ".GetDefaultModels-fm",
		"GET /api/v1/documents/:id":                                             ".GetDocumentByID-fm",
		"GET /api/v1/documents/:id/preview":                                     ".GetDocumentPreview-fm",
		"GET /api/v1/documents/artifact/:filename":                              ".GetDocumentArtifact-fm",
		"GET /api/v1/datasets/:dataset_id/documents/:document_id":               ".DownloadDocument-fm",
		"POST /api/v1/files":                                                    ".UploadFile-fm",
		"GET /api/v1/files/:id/parent":                                          ".GetParentFolder-fm",
		"GET /api/v1/datasets/:dataset_id/metadata/summary":                     ".MetadataSummaryByDataset-fm",
		"POST /api/v1/searchbots/related_questions":                             ".Handle-fm",
		"POST /api/v1/mcp/servers/import":                                       ".ImportMCPServers-fm",
		"POST /api/v1/mcp/servers/:mcp_id/test":                                 ".TestMCPServer-fm",
		"POST /api/v1/folders/:folder_id/commits":                               ".CreateCommit-fm",
		"POST /api/v1/datasets/:dataset_id/commits":                             ".CreateCommit-fm",
		"POST /api/v1/admin/canvas-runtime/:tenant_id":                          ".SetTenantRuntime-fm",
		"GET /api/v1/plugin/tools":                                              ".ListLLMTools-fm",
		"POST /api/v1/dify/retrieval":                                           ".Retrieval-fm",
		"GET /api/v1/dify/retrieval":                                            ".Retrieval-fm",
	}
	for route, method := range expected {
		if !strings.HasSuffix(routes[route], method) {
			t.Errorf("%s want %s got %s", route, method, routes[route])
		}
	}
	for _, route := range []string{"GET /api/v1/mcp/servers/:mcp_id/tools", "POST /api/v1/agents/:agent_id/upload", "POST /v1/user/login", "POST /v1/user/register", "GET /v1/system/ping", "POST /api/v1/chunk/retrieval_test"} {
		if routes[route] != "" {
			t.Error("removed route", route)
		}
	}
	for route := range expected {
		if route == "POST /api/v1/auth/login" || route == "POST /api/v1/users" {
			continue
		}
		method, path, _ := strings.Cut(route, " ")
		for _, param := range []string{"provider_name", "instance_name", "mcp_id", "agent_id", "dataset_id", "document_id", "folder_id", "tenant_id", "filename", "id"} {
			path = strings.ReplaceAll(path, ":"+param, "owned")
		}
		rec := httptest.NewRecorder()
		app.ServeHTTP(rec, httptest.NewRequest(method, path, strings.NewReader(`{}`)))
		if rec.Code != http.StatusUnauthorized {
			t.Errorf("anonymous %s: %d %s", route, rec.Code, rec.Body.String())
		}
	}
	for _, path := range []string{"/api/v1/system/ping", "/health", "/api/v1/dify/retrieval/health"} {
		rec := httptest.NewRecorder()
		app.ServeHTTP(rec, httptest.NewRequest("GET", path, nil))
		if rec.Code != 200 {
			t.Fatal(path, rec.Code)
		}
	}
}
func TestCanonicalRouterSignedAuthSQLAndRealCLI(t *testing.T) {
	f := scratch.New(t)
	t.Chdir(filepath.Join("..", ".."))
	previous := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = previous })
	config := filepath.Join(filepath.Dir(os.Getenv("MULTIRAG_GO_ALIGNMENT_CONFIG")), "service_conf.yaml")
	if err := server.Init(config); err != nil {
		t.Fatal("private canonical config", err)
	}
	cfg := server.GetConfig()
	cfg.Database = f.Config.Database
	cfg.Redis = f.Config.Redis
	if err := redisengine.Init(&cfg.Redis); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { redisengine.Close() })
	if err := server.InitVariables(redisengine.Get()); err != nil {
		t.Fatal(err)
	}
	previousStorage := storageForRouterTest(f)
	t.Cleanup(previousStorage)
	priorAdmin := local.GetAdminStatus()
	local.SetAdminStatus(0, "")
	t.Cleanup(func() { local.SetAdminStatus(priorAdmin.Status, priorAdmin.Reason) })
	if err := f.DB.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.APIToken{}, &entity.Knowledgebase{}, &entity.Document{}, &entity.Connector{}, &entity.Connector2Kb{}, &entity.File{}, &entity.File2Document{}, &entity.TenantModelProvider{}, &entity.TenantModelInstance{}, &entity.TenantModel{}, &entity.FileCommit{}, &entity.FileCommitItem{}); err != nil {
		t.Fatal(err)
	}
	active := "1"
	actors := map[string]string{}
	ids := map[string]string{}
	for _, kind := range []string{"owner", "foreign", "super"} {
		id := utility.GenerateToken()
		ids[kind] = id
		token := utility.GenerateToken()
		super := kind == "super"
		f.Journal.Event("reserve-router-actor", map[string]interface{}{"id": id, "role": kind})
		if err := f.DB.Create(&entity.User{ID: id, Email: id + "@example.test", Nickname: kind, AccessToken: &token, Status: &active, IsActive: "1", IsSuperuser: &super}).Error; err != nil {
			t.Fatal(err)
		}
		if err := f.DB.Create(&entity.Tenant{ID: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := f.DB.Create(&entity.UserTenant{ID: utility.GenerateToken(), TenantID: id, UserID: id, InvitedBy: id, Role: "owner", Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		key, err := server.GetSecretKey(redisengine.Get())
		if err != nil {
			t.Fatal(err)
		}
		signed, err := utility.DumpAccessToken(token, key)
		if err != nil {
			t.Fatal(err)
		}
		actors[kind] = signed
	}
	apiToken := "multirag-" + utility.GenerateToken()
	f.Journal.Event("reserve-router-api-token", map[string]interface{}{"tenant": ids["owner"]})
	if err := f.DB.Create(&entity.APIToken{TenantID: ids["owner"], Token: apiToken}).Error; err != nil {
		t.Fatal(err)
	}
	kbUUID, uuidErr := uuid.NewUUID()
	if uuidErr != nil {
		t.Fatal(uuidErr)
	}
	kbID := strings.ReplaceAll(kbUUID.String(), "-", "")
	f.Journal.Event("reserve-router-dataset", map[string]interface{}{"id": kbID, "tenant": ids["owner"]})
	if err := f.DB.Create(&entity.Knowledgebase{ID: kbID, TenantID: ids["owner"], CreatedBy: ids["owner"], Name: "owned router dataset", Status: &active, Permission: "me", ParserConfig: entity.JSONMap{}}).Error; err != nil {
		t.Fatal(err)
	}
	app := canonicalRouteEngine(runtime.NewSelector(f.Redis, nil))
	httpServer := testsupport.NewHTTP(t, app)
	call := func(method, path, token string, payload interface{}) (int, map[string]interface{}) {
		t.Helper()
		var reader io.Reader
		if payload != nil {
			data, err := json.Marshal(payload)
			if err != nil {
				t.Fatal(err)
			}
			reader = bytes.NewReader(data)
		}
		request, err := http.NewRequestWithContext(t.Context(), method, httpServer.URL+path, reader)
		if err != nil {
			t.Fatal(err)
		}
		request.Header.Set("Content-Type", "application/json")
		if token != "" {
			request.Header.Set("Authorization", token)
		}
		response, err := httpServer.Client().Do(request)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		var body map[string]interface{}
		if json.NewDecoder(response.Body).Decode(&body) != nil {
			t.Fatal("non-JSON route", method, path, response.StatusCode)
		}
		return response.StatusCode, body
	}
	for _, token := range []string{actors["owner"], "Bearer " + apiToken} {
		status, body := call("GET", "/api/v1/users/me", token, nil)
		if status != 200 || body["code"] != float64(0) {
			t.Fatal("signed/API auth", status, body)
		}
		profile, ok := body["data"].(map[string]interface{})
		if !ok {
			t.Fatal("missing profile")
		}
		if _, hasPassword := profile["password"]; hasPassword {
			t.Fatal("profile returned password field")
		}
		status, body = call("GET", "/api/v1/datasets/"+kbID, token, nil)
		if status != 200 || body["code"] != float64(0) {
			t.Fatal("owned dataset", status, body)
		}
	}
	status, body := call("GET", "/api/v1/datasets/"+kbID, actors["foreign"], nil)
	if status != 200 || body["code"] == float64(0) {
		t.Fatal("foreign dataset exposed", status, body)
	}
	for _, token := range []string{"", "invalid", actors["super"]} {
		status, _ := call("PUT", "/api/v1/providers/", token, map[string]interface{}{"provider_name": "OpenAI"})
		want := 401
		if token == actors["super"] {
			want = 403
		}
		if status != want {
			t.Fatal("rejected actor reached business", status, want)
		}
	}
	local.SetAdminStatus(1, "fixture unavailable")
	status, _ = call("PUT", "/api/v1/providers/", actors["owner"], map[string]interface{}{"provider_name": "OpenAI"})
	if status != 503 {
		t.Fatal("license refusal", status)
	}
	local.SetAdminStatus(0, "")
	var providerCount int
	if err := f.Read.QueryRow("SELECT COUNT(*) FROM tenant_model_provider").Scan(&providerCount); err != nil || providerCount != 0 {
		t.Fatal("denied requests changed SQL", providerCount, err)
	}
	if err := f.DB.Model(&entity.User{}).Where("id=?", ids["owner"]).Update("is_active", "0").Error; err != nil {
		t.Fatal(err)
	}
	for _, token := range []string{actors["owner"], "Bearer " + apiToken} {
		status, _ = call("GET", "/api/v1/users/me", token, nil)
		if status != 401 {
			t.Fatal("disabled auth accepted", status)
		}
	}
	if err := f.DB.Model(&entity.User{}).Where("id=?", ids["owner"]).Update("is_active", "1").Error; err != nil {
		t.Fatal(err)
	}

	f.Journal.Event("reserve-signed-runtime-override", map[string]interface{}{"key": "tenant_canvas_runtime:" + ids["owner"]})
	status, body = call("POST", "/api/v1/admin/canvas-runtime/"+ids["owner"], actors["owner"], map[string]interface{}{"runtime": "go"})
	if status != 200 || body["code"] != float64(0) {
		t.Fatal("owner runtime selection", status, body)
	}
	status, _ = call("POST", "/api/v1/admin/canvas-runtime/"+ids["owner"], actors["foreign"], map[string]interface{}{"runtime": "python"})
	if status != 403 {
		t.Fatal("foreign runtime selection", status)
	}
	if got, err := f.Redis.Get(t.Context(), "tenant_canvas_runtime:"+ids["owner"]).Result(); err != nil || got != "go" {
		t.Fatal("denied runtime changed Redis", got, err)
	}

	folderID, fileID := utility.GenerateToken(), utility.GenerateToken()
	f.Journal.Event("reserve-router-workspace", map[string]interface{}{"folder": folderID, "file": fileID, "tenant": ids["owner"]})
	for _, row := range []interface{}{&entity.File{ID: folderID, ParentID: folderID, TenantID: ids["owner"], CreatedBy: ids["owner"], Name: "owned folder", Type: "folder"}, &entity.File{ID: fileID, ParentID: folderID, TenantID: ids["owner"], CreatedBy: ids["owner"], Name: "note.txt", Type: "file", SourceType: ""}} {
		if err := f.DB.Create(row).Error; err != nil {
			t.Fatal(err)
		}
	}
	callback := "reserve_router_commit_" + f.Prefix
	if err := f.DB.Callback().Create().Before("gorm:create").Register(callback, func(tx *gorm.DB) {
		value := reflect.Indirect(tx.Statement.ReflectValue)
		if value.IsValid() && value.Kind() == reflect.Struct {
			if id := value.FieldByName("ID"); id.IsValid() && id.Kind() == reflect.String {
				f.Journal.Event("reserve-router-created-row", map[string]interface{}{"table": tx.Statement.Table, "id": id.String()})
			}
		}
	}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = f.DB.Callback().Create().Remove(callback) })
	var commitIDs []string
	for index, content := range []string{"first snapshot", "second snapshot"} {
		operation := "add"
		if index > 0 {
			operation = "modify"
		}
		status, response := call("POST", "/api/v1/folders/"+folderID+"/commits", actors["owner"], map[string]interface{}{"message": "snapshot", "files": []entity.FileChange{{FileID: fileID, FileName: "note.txt", Operation: operation, Content: content}}})
		if status != 200 || response["code"] != float64(0) {
			t.Fatal("actual commit", status, response)
		}
		data, ok := response["data"].(map[string]interface{})
		if !ok {
			t.Fatal("commit response")
		}
		commitID, ok := data["id"].(string)
		if !ok || commitID == "" {
			t.Fatal("commit identity")
		}
		commitIDs = append(commitIDs, commitID)
		var location string
		if err := f.Read.QueryRow("SELECT new_location FROM file_commit_item WHERE commit_id=? AND file_id=?", commitID, fileID).Scan(&location); err != nil {
			t.Fatal(err)
		}
		f.ReadObject(t, folderID, location, []byte(content))
	}
	var oldLocation string
	if err := f.Read.QueryRow("SELECT new_location FROM file_commit_item WHERE commit_id=? AND file_id=?", commitIDs[0], fileID).Scan(&oldLocation); err != nil {
		t.Fatal(err)
	}
	f.ReadObject(t, folderID, oldLocation, []byte("first snapshot"))
	beforeCalls := f.Store.Calls
	status, body = call("POST", "/api/v1/folders/"+folderID+"/commits", actors["foreign"], map[string]interface{}{"message": "denied", "files": []entity.FileChange{{FileID: fileID, Operation: "modify", Content: "foreign"}}})
	if status != 200 || body["code"] == float64(0) || f.Store.Calls != beforeCalls {
		t.Fatal("foreign commit had effects", status, body)
	}
	f.Journal.Event("reserve-router-commit-failure-trigger", map[string]interface{}{"database": f.Database, "trigger": "reject_router_commit_item"})
	if err := f.DB.Exec("CREATE TRIGGER reject_router_commit_item BEFORE INSERT ON file_commit_item FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='controlled commit item failure'").Error; err != nil {
		t.Fatal(err)
	}
	status, body = call("POST", "/api/v1/folders/"+folderID+"/commits", actors["owner"], map[string]interface{}{"message": "failed", "files": []entity.FileChange{{FileID: fileID, Operation: "modify", Content: "failed snapshot"}}})
	if body["code"] == float64(0) {
		t.Fatal("failed SQL commit returned success", status)
	}
	var commitCount int
	if err := f.Read.QueryRow("SELECT COUNT(*) FROM file_commit WHERE folder_id=?", folderID).Scan(&commitCount); err != nil || commitCount != 2 {
		t.Fatal("failed transaction left SQL", commitCount, err)
	}
	hash := sha256.Sum256([]byte("failed snapshot"))
	failedKey := folderID + "/.objects/" + hex.EncodeToString(hash[:])
	reader, err := minio.New(f.Config.MinIO.Host, &minio.Options{Creds: credentials.NewStaticV4(f.Config.MinIO.User, f.Config.MinIO.Password, ""), Secure: false, Region: f.Config.MinIO.Region})
	if err != nil {
		t.Fatal(err)
	}
	_, err = reader.StatObject(t.Context(), f.Bucket, failedKey, minio.StatObjectOptions{})
	if err == nil || minio.ToErrorResponse(err).Code != "NoSuchKey" {
		t.Fatal("independent failed snapshot cleanup", err)
	}
	f.ReadObject(t, folderID, oldLocation, []byte("first snapshot"))
	verifyRouterCLI(t, f, httpServer.URL, apiToken)
}

func storageForRouterTest(f *scratch.Services) func() {
	factory := storage.GetStorageFactory()
	prior := factory.GetStorage()
	factory.SetStorage(f.Store)
	return func() { factory.SetStorage(prior) }
}
func verifyRouterCLI(t *testing.T, f *scratch.Services, baseURL, token string) {
	t.Helper()
	binary := os.Getenv("MULTIRAG_M_CLI")
	if binary == "" {
		t.Fatal("actual candidate CLI required")
	}
	path := filepath.Join(os.Getenv("TMPDIR"), "router-"+f.Prefix+".yml")
	f.Journal.Event("reserve-router-cli-config", map[string]interface{}{"path": path})
	raw, _ := json.Marshal(map[string]interface{}{"host": strings.TrimPrefix(baseURL, "http://"), "api_token": token})
	if err := os.WriteFile(path, raw, 0600); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.Remove(path); err != nil {
			t.Error(err)
		}
	})
	f.Journal.Event("reserve-router-real-cli", map[string]interface{}{"binary": binary, "arguments": []string{"-f", path, "LIST DATASETS;"}})
	child := exec.CommandContext(t.Context(), binary, "-f", path, "LIST DATASETS;")
	output, err := child.CombinedOutput()
	if err != nil || !bytes.Contains(output, []byte("owned router dataset")) || bytes.Contains(output, []byte("ERROR")) {
		t.Fatalf("actual CLI route consumer failed: %v %s", err, output)
	}
}
