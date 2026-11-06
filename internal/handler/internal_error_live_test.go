//go:build integration

package handler_test

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"gorm.io/gorm"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/storage"
	"multirag/internal/testsupport"
	"multirag/internal/testsupport/scratch"
	"multirag/internal/utility"
)

var controlledHandlerFailure = errors.New("controlled internal failure password=private-marker host=private-backend")

type failingHandlerGraph struct {
	engine.DocEngine
	stage atomic.Value
	calls atomic.Int64
}

func (e *failingHandlerGraph) ChunkStoreExists(context.Context, string, string) (bool, error) {
	e.calls.Add(1)
	if e.stage.Load() == "exists" {
		return false, controlledHandlerFailure
	}
	return true, nil
}

func (e *failingHandlerGraph) Search(_ context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	e.calls.Add(1)
	if e.stage.Load() == "sort" {
		return &types.SearchResult{Chunks: []map[string]interface{}{{"kb_id": req.KbIDs[0], "knowledge_graph_kwd": "graph", "content_with_weight": `{"nodes":[{"id":"a","pagerank":"private-marker"}],"edges":[]}`}}}, nil
	}
	return nil, controlledHandlerFailure
}

func (e *failingHandlerGraph) DeleteChunks(context.Context, map[string]interface{}, string, string) (int64, error) {
	e.calls.Add(1)
	return 0, controlledHandlerFailure
}

type failingHandlerStorage struct {
	storage.Storage
}

func (s *failingHandlerStorage) Get(string, string, ...string) ([]byte, error) {
	return nil, controlledHandlerFailure
}

func TestHandlerInternalErrorsSQLHTTP(t *testing.T) {
	f := scratch.New(t)
	previousDB := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = previousDB })
	models := []any{&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.APIToken{}, &entity.Knowledgebase{}, &entity.Document{}, &entity.Connector{}, &entity.Connector2Kb{}, &entity.File{}, &entity.File2Document{}}
	if err := f.DB.AutoMigrate(models...); err != nil {
		t.Fatal(err)
	}
	owner, outsider := f.Prefix+"owner", f.Prefix+"outsider"
	active, location := "1", "sentinel"
	for _, id := range []string{owner, outsider} {
		token := strings.Repeat("x", 40)
		if err := f.DB.Create(&entity.User{ID: id, Email: id + "@errors.invalid", Nickname: id, Status: &active, AccessToken: &token, IsActive: true, IsAuthenticated: true}).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := f.DB.Create(&entity.Tenant{ID: owner, Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Create(&entity.UserTenant{ID: f.Prefix, UserID: owner, TenantID: owner, InvitedBy: owner, Role: "owner", Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	kbID := strings.ReplaceAll(uuid.Must(uuid.NewUUID()).String(), "-", "")
	if err := f.DB.Create(&entity.Knowledgebase{ID: kbID, Name: "owned", TenantID: owner, CreatedBy: owner, Permission: "me", Status: &active, ParserConfig: entity.JSONMap{}}).Error; err != nil {
		t.Fatal(err)
	}
	fileID := f.Prefix + "file"
	if err := f.DB.Create(&entity.File{ID: fileID, ParentID: fileID, TenantID: owner, CreatedBy: owner, Name: "sentinel.txt", Type: "doc", Location: &location}).Error; err != nil {
		t.Fatal(err)
	}
	managedID := f.Prefix + "managed"
	if err := f.DB.Create(&entity.File{ID: managedID, ParentID: managedID, TenantID: owner, CreatedBy: owner, Name: "managed.txt", Type: "doc", SourceType: "skill_file"}).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Create(&entity.File2Document{ID: f.Prefix, FileID: &fileID}).Error; err != nil {
		t.Fatal(err)
	}
	want := []byte("unchanged controlled object")
	if err := f.Store.Put(fileID, location, want); err != nil {
		t.Fatal(err)
	}
	f.ReadObject(t, fileID, location, want)
	storeFactory := storage.GetStorageFactory()
	previousStore := storeFactory.GetStorage()
	storeFactory.SetStorage(&failingHandlerStorage{Storage: f.Store})
	t.Cleanup(func() { storeFactory.SetStorage(previousStore) })
	previousSecret := server.GetSecretKey()
	if err := server.InitVariables(statusSigningStore{secret: f.Prefix + "controlled-signing-key"}); err != nil {
		t.Fatal(err)
	}
	server.SetSecretKey(f.Prefix + "controlled-signing-key")
	t.Cleanup(func() { server.SetSecretKey(previousSecret) })
	previousAdmin := local.GetAdminStatus()
	local.SetAdminStatus(0, "controlled error regression")
	t.Cleanup(func() { local.SetAdminStatus(previousAdmin.Status, previousAdmin.Reason) })
	tokens := map[string]string{}
	for _, id := range []string{owner, outsider} {
		var err error
		tokens[id], err = utility.SignUserJWT(id+"@errors.invalid", server.GetSecretKey())
		if err != nil {
			t.Fatal(err)
		}
	}
	graph := &failingHandlerGraph{}
	app := gin.New()
	appRouter := router.NewRouter(handler.NewAuthHandler(), nil, nil, nil, handler.NewDatasetsHandler(service.NewDatasetsService(), graph), nil, nil, nil, nil, nil, nil, nil, handler.NewFileHandler(service.NewFileService(), service.NewUserService()), nil, nil, nil)
	appRouter.Setup(app)
	httpServer := testsupport.NewHTTP(t, app)
	request := func(method, path, actor string, status int, code common.ErrorCode, message string, payload ...string) {
		t.Helper()
		var input io.Reader
		if len(payload) > 0 {
			input = strings.NewReader(payload[0])
		}
		req, err := http.NewRequest(method, httpServer.URL+path, input)
		if err != nil {
			t.Fatal(err)
		}
		if actor != "" {
			req.Header.Set("Authorization", "Bearer "+tokens[actor])
		}
		if len(payload) == 2 {
			req.Header.Set("Content-Type", payload[1])
		}
		resp, err := httpServer.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		raw, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatal(err)
		}
		var body struct {
			Code    common.ErrorCode
			Message string
			Data    any
		}
		if err := json.Unmarshal(raw, &body); err != nil {
			t.Fatal(err)
		}
		if resp.StatusCode != status || body.Code != code || (message != "" && body.Message != message) || body.Data != nil || strings.Contains(string(raw), "private-marker") || strings.Contains(string(raw), "private-backend") {
			t.Fatalf("%s %s: HTTP %d %s", method, path, resp.StatusCode, raw)
		}
	}
	graphPath := "/api/v1/datasets/" + kbID + "/graph"
	for _, method := range []string{http.MethodGet, http.MethodDelete} {
		request(method, graphPath, "", 401, common.CodeUnauthorized, "Missing Authorization header")
		request(method, graphPath, outsider, 200, common.CodeForbidden, service.ErrDatasetAccessDenied.Error())
	}
	for _, path := range []string{"/api/v1/files", "/v1/file/root_folder", "/api/v1/files/" + fileID, "/api/v1/files/" + fileID + "/ancestors"} {
		request(http.MethodGet, path, "", 401, common.CodeUnauthorized, "Missing Authorization header")
	}
	request(http.MethodPost, "/api/v1/files", "", 401, common.CodeUnauthorized, "Missing Authorization header", `{"name":"unauthorized folder"}`, "application/json")
	request(http.MethodGet, "/api/v1/files/"+fileID+"/ancestors", outsider, 200, common.CodeDataError, service.ErrFileNoAuthorization.Error())
	request(http.MethodGet, "/api/v1/files/"+fileID, outsider, 200, common.CodeUnauthorized, "No authorization.")
	request(http.MethodGet, "/api/v1/files/missing", owner, 200, common.CodeUnauthorized, "Document not found!")
	for _, path := range []string{"/api/v1/files/" + managedID, "/api/v1/files/" + managedID + "/ancestors", "/api/v1/files?parent_id=" + managedID} {
		request(http.MethodGet, path, owner, 404, common.ErrorCode(404), "Not found")
	}
	if graph.calls.Load() != 0 {
		t.Fatal("denied Router requests reached graph engine")
	}
	for _, stage := range []string{"exists", "search", "sort", "delete"} {
		graph.stage.Store(stage)
		method := http.MethodGet
		if stage == "delete" {
			method = http.MethodDelete
		}
		request(method, graphPath, owner, 200, common.CodeServerError, common.CodeServerError.Message())
	}
	request(http.MethodGet, "/api/v1/files/"+fileID, owner, 200, common.CodeServerError, common.CodeServerError.Message())
	// Fail only the file boundary after the real Router has authenticated and
	// authorized the user. No SQL statement at this boundary may succeed.
	failFile := func(tx *gorm.DB) {
		if tx.Statement.Table == "t_ai_files" {
			tx.AddError(controlledHandlerFailure)
		}
	}
	if err := f.DB.Callback().Query().Before("gorm:query").Register("handler-private-file-query", failFile); err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Callback().Create().Before("gorm:before_create").Register("handler-private-file-create", failFile); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{"/api/v1/files?parent_id=" + fileID, "/v1/file/root_folder", "/api/v1/files/" + fileID + "/parent", "/api/v1/files/" + fileID + "/ancestors", "/v1/file/all_parent_folder?file_id=" + fileID} {
		request(http.MethodGet, path, owner, 200, common.CodeServerError, common.CodeServerError.Message())
	}
	request(http.MethodPost, "/api/v1/files", owner, 200, common.CodeServerError, common.CodeServerError.Message(), `{"name":"new folder"}`, "application/json")
	request(http.MethodPost, "/api/v1/files", owner, 200, common.CodeServerError, common.CodeServerError.Message(), "--owned-boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.txt\"\r\n\r\nhello\r\n--owned-boundary--\r\n", "multipart/form-data; boundary=owned-boundary")
	f.DB.Callback().Query().Remove("handler-private-file-query")
	f.DB.Callback().Create().Remove("handler-private-file-create")
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM t_ai_files").Scan(&count); err != nil || count != 2 {
		t.Fatalf("file failure side effects: count=%d err=%v", count, err)
	}
	if err := f.Read.QueryRow("SELECT count(*) FROM t_ai_knowledgebases").Scan(&count); err != nil || count != 1 {
		t.Fatalf("graph failure side effects: count=%d err=%v", count, err)
	}
	f.ReadObject(t, fileID, location, want)
	if keys := f.ObjectKeys(t); len(keys) != 1 {
		t.Fatalf("unexpected object side effects: %v", keys)
	}
	f.Journal.Event("internal-error-independent-readback", map[string]any{"sql_unchanged": true, "object_unchanged": true, "denied_engine_calls": 0, "graph_calls": graph.calls.Load()})
}
