package router

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	rawredis "github.com/redis/go-redis/v9"
	"go.uber.org/zap"
	"gorm.io/gorm"
	"multirag/internal/common"
	"multirag/internal/dao"
	redisengine "multirag/internal/engine/redis"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/testsupport"
	"multirag/internal/testsupport/scratch"
	"multirag/internal/utility"
)

// after22SharedVariables keeps signing-key initialization off shared Redis.
// Authentication still uses the production JWT verifier and SQL user lookup.
type after22SharedVariables struct{ key string }

func (s *after22SharedVariables) Get(string) (string, error) { return s.key, nil }
func (s *after22SharedVariables) Set(_ string, value string, _ time.Duration) bool {
	s.key = value
	return true
}
func (s *after22SharedVariables) SetNX(_ string, value string, _ time.Duration) bool {
	if s.key != "" {
		return false
	}
	s.key = value
	return true
}

// TestAfter22SharedRouterJWTSQLObjects exercises the actual Router.Setup and
// signed JWT middleware. PostgreSQL, MinIO and HTTP are real owned resources;
// only the remote OpenAI origin is replaced by an owned protocol fixture.
func TestAfter22SharedRouterJWTSQLObjects(t *testing.T) {
	env := scratch.New(t)
	previousDB, previousLogger := dao.DB, common.Logger
	previousKey, previousAdmin, previousMode := server.GetSecretKey(), local.GetAdminStatus(), gin.Mode()
	dao.DB, common.Logger = env.DB, zap.NewNop()
	t.Cleanup(func() {
		server.SetSecretKey(previousKey)
		local.SetAdminStatus(previousAdmin.Status, previousAdmin.Reason)
		gin.SetMode(previousMode)
		dao.DB, common.Logger = previousDB, previousLogger
	})
	key := uuid.NewString() + uuid.NewString()
	env.Journal.Event("reserve-process-signing-key", map[string]any{"storage": "process memory", "shared_redis": false})
	if err := server.InitVariables(&after22SharedVariables{key: key}); err != nil {
		t.Fatal("initialize process signing variables")
	}
	server.SetSecretKey(key)
	local.SetAdminStatus(0, "owned after22 shared router acceptance")
	gin.SetMode(gin.TestMode)

	projectRoot := filepath.Join("..", "..")
	if _, err := os.Stat("go.mod"); err == nil {
		projectRoot = "."
	}
	t.Chdir(projectRoot)
	env.Journal.Event("reserve-shared-schema", map[string]any{"database": env.Database, "schema": "usr_ai", "tables": []string{
		"t_ai_users", "t_ai_tenants", "t_ai_user_tenants", "t_ai_tenant_llms", "tenant_model_provider", "tenant_model_instance", "tenant_model",
		"t_ai_files", "t_ai_file2documents", "t_ai_file_commits", "t_ai_file_commit_items",
		"t_ai_user_canvases", "t_ai_user_canvas_version",
	}})
	if err := env.DB.AutoMigrate(
		&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.TenantLLM{},
		&entity.TenantModelProvider{}, &entity.TenantModelInstance{}, &entity.TenantModel{},
		&entity.File{}, &entity.File2Document{}, &entity.FileCommit{}, &entity.FileCommitItem{},
		&entity.UserCanvas{}, &entity.UserCanvasVersion{},
	); err != nil {
		t.Fatal("initialize owned shared-contract tables")
	}
	// Reserve generated FileCommit/FileCommitItem identities as well as fixtures
	// before GORM reaches the SQL INSERT. No credential fields enter the journal.
	callbackName := env.Prefix + "_shared_rows"
	if err := env.DB.Callback().Create().Before("gorm:create").Register(callbackName, func(tx *gorm.DB) {
		ids := []any{}
		var collect func(reflect.Value)
		collect = func(value reflect.Value) {
			if !value.IsValid() {
				return
			}
			if value.Kind() == reflect.Pointer || value.Kind() == reflect.Interface {
				if !value.IsNil() {
					collect(value.Elem())
				}
				return
			}
			if value.Kind() == reflect.Slice || value.Kind() == reflect.Array {
				for i := 0; i < value.Len(); i++ {
					collect(value.Index(i))
				}
			} else if value.Kind() == reflect.Struct {
				if id := value.FieldByName("ID"); id.IsValid() && id.CanInterface() {
					ids = append(ids, id.Interface())
				}
			}
		}
		collect(tx.Statement.ReflectValue)
		env.Journal.Event("reserve-shared-SQL-rows", map[string]any{"table": tx.Statement.Table, "ids": ids})
	}); err != nil {
		t.Fatal("register owned SQL row reservation")
	}
	t.Cleanup(func() { _ = env.DB.Callback().Create().Remove(callbackName) })

	owner, foreign, rootID, folderID, fileID := after22SharedID(), after22SharedID(), after22SharedID(), after22SharedID(), after22SharedID()
	active, modelType, modelName, credential := "1", "chat", "gpt-4o", "after22-owned-provider-key"
	create := func(row any) {
		t.Helper()
		if err := env.DB.Create(row).Error; err != nil {
			t.Fatalf("create owned fixture row (%T)", row)
		}
	}
	tokens := map[string]string{}
	for _, id := range []string{owner, foreign} {
		create(&entity.User{ID: id, Email: id + "@after22.invalid", Nickname: "owned shared router", Status: &active, IsActive: true, IsAuthenticated: true})
		create(&entity.Tenant{ID: id, Status: &active})
		create(&entity.UserTenant{ID: after22SharedID(), UserID: id, TenantID: id, InvitedBy: id, Role: "owner", Status: &active})
		token, err := utility.SignUserJWT(id+"@after22.invalid", server.GetSecretKey())
		if err != nil {
			t.Fatal("sign actual user JWT")
		}
		tokens[id] = token
	}
	create(&entity.File{ID: rootID, ParentID: rootID, TenantID: owner, CreatedBy: owner, Name: "/", Type: "folder"})
	create(&entity.File{ID: folderID, ParentID: rootID, TenantID: owner, CreatedBy: owner, Name: "owned workspace", Type: "folder"})

	var providerCalls atomic.Int32
	vendor := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		providerCalls.Add(1)
		w.Header().Set("Content-Type", "application/json")
		if req.Method != http.MethodGet || req.URL.Path != "/v1/models" {
			t.Error("provider driver used an unexpected method or path")
			w.WriteHeader(http.StatusNotFound)
			_, _ = io.WriteString(w, `{"error":{"message":"owned route mismatch"}}`)
			return
		}
		if req.Header.Get("Authorization") != "Bearer "+credential {
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = io.WriteString(w, `{"error":{"message":"owned credential rejected"}}`)
			return
		}
		_, _ = io.WriteString(w, `{"object":"list","data":[{"id":"gpt-4o","object":"model"}]}`)
	}))
	baseURL := vendor.URL + "/v1"
	create(&entity.TenantLLM{ID: 1001, TenantID: owner, LLMFactory: "OpenAI", LLMName: &modelName, ModelType: &modelType, APIKey: &credential, APIBase: &baseURL, Status: active})
	create(&entity.TenantLLM{ID: 1002, TenantID: foreign, LLMFactory: "OpenAI", LLMName: &modelName, ModelType: &modelType, APIKey: &credential, APIBase: &baseURL, Status: active})
	catalog, err := entity.NewProviderManager(filepath.Join("configs", "models"))
	if err != nil || catalog.FindProvider("OpenAI") == nil {
		t.Fatal("load actual OpenAI provider catalog")
	}
	// Replace only this private catalog's origin, including its fallback URL, so
	// a regression cannot send the fixture credential to a public provider.
	catalog.FindProvider("OpenAI").URL = map[string]string{"default": baseURL}
	models := service.NewModelProviderServiceWithCatalog(catalog, nil)
	r := NewRouter(handler.NewAuthHandler(), nil, handler.NewTenantHandler(service.NewTenantServiceWithModels(models), nil), nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, handler.NewProviderHandler(nil, models), nil)
	r.SetFileCommitHandler(handler.NewFileCommitHandler(&service.FileCommitService{DB: env.DB, Store: env.Store}))
	app := gin.New()
	r.Setup(app)
	for _, retired := range []string{"PATCH /api/v1/models", "GET /api/v1/providers/:provider_name/connection"} {
		for _, route := range app.Routes() {
			if route.Method+" "+route.Path == retired {
				t.Fatalf("retired shared route remains: %s", retired)
			}
		}
	}
	api := testsupport.NewHTTP(t, app)
	client := api.Client()
	client.Timeout = 10 * time.Second
	call := func(t *testing.T, actor, method, path string, payload any) (int, []byte) {
		t.Helper()
		var body io.Reader
		if payload != nil {
			raw, err := json.Marshal(payload)
			if err != nil {
				t.Fatal("encode shared-contract request")
			}
			body = bytes.NewReader(raw)
		}
		req, err := http.NewRequestWithContext(t.Context(), method, api.URL+path, body)
		if err != nil {
			t.Fatal("construct shared-contract request")
		}
		req.Header.Set("Content-Type", "application/json")
		if actor != "" {
			req.Header.Set("Authorization", "Bearer "+tokens[actor])
		}
		response, err := client.Do(req)
		if err != nil {
			t.Fatal("shared-contract HTTP request failed")
		}
		defer response.Body.Close()
		raw, err := io.ReadAll(io.LimitReader(response.Body, 2<<20))
		if err != nil {
			t.Fatal("read shared-contract response")
		}
		if bytes.Contains(raw, []byte(credential)) || bytes.Contains(raw, []byte(baseURL)) {
			t.Fatal("shared route exposed provider credentials or private endpoint")
		}
		return response.StatusCode, raw
	}
	success := func(t *testing.T, status int, raw []byte, destination any) {
		t.Helper()
		var result struct {
			Code *common.ErrorCode `json:"code"`
			Data json.RawMessage   `json:"data"`
		}
		if status != http.StatusOK || json.Unmarshal(raw, &result) != nil || result.Code == nil || *result.Code != common.CodeSuccess {
			t.Fatalf("shared route did not succeed: status=%d", status)
		}
		if destination != nil && json.Unmarshal(result.Data, destination) != nil {
			t.Fatal("shared route returned the wrong data contract")
		}
	}

	t.Run("model_inventory_and_numeric_defaults", func(t *testing.T) {
		status, raw := call(t, "", http.MethodGet, "/api/v1/models", nil)
		if status != http.StatusUnauthorized {
			t.Fatal("model inventory bypassed real JWT authentication")
		}
		status, raw = call(t, owner, http.MethodGet, "/api/v1/models", nil)
		var inventory []struct {
			ID   int64  `json:"id"`
			Name string `json:"model_name"`
		}
		success(t, status, raw, &inventory)
		if len(inventory) != 1 || inventory[0].ID != 1001 || inventory[0].Name != modelName {
			t.Fatal("GET /models did not return the scoped numeric binding inventory")
		}
		status, raw = call(t, owner, http.MethodPatch, "/api/v1/models/default", map[string]any{"model_id": 1001, "model_type": "chat"})
		success(t, status, raw, nil)
		var selected int64
		if err := env.Read.QueryRow("SELECT tenant_llm_id FROM t_ai_tenants WHERE id=$1", owner).Scan(&selected); err != nil || selected != 1001 {
			t.Fatal("numeric default was not independently committed")
		}
		status, raw = call(t, owner, http.MethodGet, "/api/v1/models/default", nil)
		var defaults struct {
			Models []service.ModelItem `json:"models"`
		}
		success(t, status, raw, &defaults)
		found := false
		for _, item := range defaults.Models {
			if item.ModelType == "llm" && item.ModelID != nil && *item.ModelID == 1001 && item.ModelName != nil && *item.ModelName == modelName && item.Enable {
				found = true
			}
		}
		if !found {
			t.Fatal("GET /models/default lost the committed numeric binding")
		}
		before := after22SharedSQLFingerprint(t, env.Read)
		for _, actor := range []string{owner, foreign} {
			id := 1002
			if actor == foreign {
				id = 1001
			}
			status, raw = call(t, actor, http.MethodPatch, "/api/v1/models/default", map[string]any{"model_id": id, "model_type": "chat"})
			var result struct {
				Code common.ErrorCode `json:"code"`
			}
			if json.Unmarshal(raw, &result) != nil || result.Code == common.CodeSuccess || status != http.StatusOK || after22SharedSQLFingerprint(t, env.Read) != before {
				t.Fatal("cross-tenant numeric selection succeeded or changed SQL")
			}
		}
		status, _ = call(t, owner, http.MethodPatch, "/api/v1/models", map[string]any{"model_id": 1001, "model_type": "chat"})
		if status != http.StatusNotFound || after22SharedSQLFingerprint(t, env.Read) != before {
			t.Fatal("retired PATCH /models remains writable")
		}
	})

	t.Run("provider_connection_uses_real_openai_driver", func(t *testing.T) {
		before, callsBefore := after22SharedSQLFingerprint(t, env.Read), providerCalls.Load()
		status, raw := call(t, owner, http.MethodPost, "/api/v1/providers/OpenAI/connection", service.CheckConnectionRequest{APIKey: &credential, BaseURL: baseURL})
		success(t, status, raw, nil)
		if providerCalls.Load() != callsBefore+1 || after22SharedSQLFingerprint(t, env.Read) != before {
			t.Fatal("provider connection did not probe the actual driver without SQL writes")
		}
		wrong := "after22-rejected-key"
		status, raw = call(t, owner, http.MethodPost, "/api/v1/providers/OpenAI/connection", service.CheckConnectionRequest{APIKey: &wrong, BaseURL: baseURL})
		var result struct {
			Code common.ErrorCode `json:"code"`
		}
		if status != http.StatusOK || json.Unmarshal(raw, &result) != nil || result.Code == common.CodeSuccess || providerCalls.Load() != callsBefore+2 || after22SharedSQLFingerprint(t, env.Read) != before {
			t.Fatal("rejected provider credential became a success or changed SQL")
		}
		status, _ = call(t, owner, http.MethodGet, "/api/v1/providers/OpenAI/connection", nil)
		if status != http.StatusNotFound || providerCalls.Load() != callsBefore+2 || after22SharedSQLFingerprint(t, env.Read) != before {
			t.Fatal("retired GET provider connection still probes or writes")
		}
	})

	t.Run("file_commits_jwt_sql_objects_and_foreign_denial", func(t *testing.T) {
		path := "/api/v1/workspaces/" + folderID
		firstText, secondText, name := "router historical bytes ✓", "router replacement bytes ✓", "owned.txt"
		status, raw := call(t, owner, http.MethodPost, path+"/commits", entity.CreateFileCommit{Message: "owned first snapshot", Files: []entity.FileChange{{FileID: fileID, Operation: "add", FileName: &name, Content: &firstText}}})
		var first entity.FileCommit
		success(t, status, raw, &first)
		if first.ID == "" || first.FolderID != folderID || first.AuthorID != owner || first.Sequence != 1 {
			t.Fatal("actual FileCommit route returned an invalid first snapshot")
		}
		readSnapshot := func(id, want string) entity.SnapshotEntry {
			t.Helper()
			var tenant, folder, author, tree string
			if err := env.Read.QueryRow("SELECT tenant_id,folder_id,author_id,tree_state::text FROM t_ai_file_commits WHERE id=$1", id).Scan(&tenant, &folder, &author, &tree); err != nil || tenant != owner || folder != folderID || author != owner {
				t.Fatal("independent snapshot SQL identity readback failed")
			}
			var state entity.SnapshotState
			if json.Unmarshal([]byte(tree), &state) != nil {
				t.Fatal("invalid independently persisted snapshot tree")
			}
			entry, exists := state[fileID]
			if !exists || entry.Hash != fmt.Sprintf("%x", sha256.Sum256([]byte(want))) || entry.Size != int64(len(want)) || !strings.HasPrefix(entry.Location, ".file-commits/"+owner+"/"+folderID+"/") {
				t.Fatal("immutable snapshot identity or hash changed")
			}
			env.ReadObject(t, entry.Bucket, entry.Location, []byte(want))
			return entry
		}
		initial := readSnapshot(first.ID, firstText)
		status, raw = call(t, owner, http.MethodPost, path+"/commits", map[string]any{"message": "owned second snapshot", "parent_id": first.ID, "files": []entity.FileChange{{FileID: fileID, Operation: "modify", Content: &secondText}}})
		var second entity.FileCommit
		success(t, status, raw, &second)
		if second.Sequence != 2 || second.ParentID == nil || *second.ParentID != first.ID {
			t.Fatal("Router FileCommit did not extend the SQL history chain")
		}
		latest := readSnapshot(second.ID, secondText)
		var parent, working string
		if err := env.Read.QueryRow("SELECT parent_id,location FROM t_ai_files WHERE id=$1 AND tenant_id=$2", fileID, owner).Scan(&parent, &working); err != nil || !strings.HasPrefix(working, ".file-working/") || working == latest.Location || initial.Location == latest.Location {
			t.Fatal("live file aliases historical bytes or lost its own working key")
		}
		env.ReadObject(t, parent, working, []byte(secondText))
		var operation, itemLocation string
		if err := env.Read.QueryRow("SELECT operation,new_location FROM t_ai_file_commit_items WHERE commit_id=$1 AND file_id=$2", second.ID, fileID).Scan(&operation, &itemLocation); err != nil || operation != "modify" || itemLocation != latest.Location {
			t.Fatal("snapshot item SQL did not reference the immutable content")
		}
		status, raw = call(t, owner, http.MethodGet, path+"/commits/"+first.ID+"/files/"+fileID+"/content", nil)
		var content struct {
			Content string `json:"content"`
		}
		success(t, status, raw, &content)
		if content.Content != firstText {
			t.Fatal("historical HTTP content changed after a live modification")
		}
		status, raw = call(t, owner, http.MethodGet, path+"/commits", nil)
		var history struct {
			Total int64               `json:"total"`
			Items []entity.FileCommit `json:"commits"`
		}
		success(t, status, raw, &history)
		if history.Total != 2 || len(history.Items) != 2 || history.Items[0].ID != second.ID || history.Items[1].ID != first.ID {
			t.Fatal("workspace history is not backed by the committed SQL chain")
		}
		before, keys := after22SharedSQLFingerprint(t, env.Read), env.ObjectKeys(t)
		status, _ = call(t, foreign, http.MethodPost, path+"/commits", entity.CreateFileCommit{Message: "must not write", Files: []entity.FileChange{}})
		if status != http.StatusNotFound {
			t.Fatal("foreign JWT created a workspace snapshot")
		}
		for _, suffix := range []string{"/commits", "/commits/" + first.ID + "/files/" + fileID + "/content"} {
			status, _ = call(t, foreign, http.MethodGet, path+suffix, nil)
			if status != http.StatusNotFound {
				t.Fatal("foreign JWT read another tenant's snapshot")
			}
		}
		status, _ = call(t, foreign, http.MethodGet, "/api/v1/files/"+fileID+"/versions", nil)
		if status != http.StatusNotFound || after22SharedSQLFingerprint(t, env.Read) != before || !reflect.DeepEqual(env.ObjectKeys(t), keys) {
			t.Fatal("foreign snapshot denial leaked versions or changed SQL/objects")
		}
		env.Journal.Event("shared-router-readback", map[string]any{"JWT": "actual middleware", "first_commit": first.ID, "second_commit": second.ID, "history_content_preserved": true, "foreign_SQL_unchanged": true, "foreign_objects_unchanged": true})
	})

	t.Run("agent_helpers_jwt_echo_sql_and_redis", func(t *testing.T) {
		// The production constructor reads this engine's published client. Do not
		// replace or close another test's live client; preceding owners must Close.
		if redisengine.Get() != nil {
			t.Fatal("previous Redis engine owner must close its client before this case")
		}
		if err := redisengine.Init(&env.Config.Redis); err != nil {
			t.Fatal("initialize actual Redis engine for public Agent execution")
		}
		client := redisengine.Get().GetClient()
		options := *env.Redis.Options()
		reader := rawredis.NewClient(&options)
		// Subtest cleanup runs before scratch.New's parent cleanup. LIFO keeps
		// both clients alive for exact-key deletion and independent absence reads.
		t.Cleanup(func() {
			if err := redisengine.Close(); err != nil {
				t.Error("close owned Agent Redis engine")
			}
			if err := reader.Close(); err != nil {
				t.Error("close independent Agent Redis reader")
			}
		})
		hook := &after22SharedRunJournal{journal: env.Journal, reader: reader, keys: map[string]bool{}}
		client.AddHook(hook)
		t.Cleanup(func() {
			keys := hook.reservedKeys()
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			var remaining int64
			var cleanupErr, readErr error
			if len(keys) > 0 {
				cleanupErr = client.Del(ctx, keys...).Err()
				remaining, readErr = reader.Exists(ctx, keys...).Result()
			}
			env.Journal.Event("shared-agent-redis-cleanup-readback", map[string]any{"keys": keys, "remaining": remaining, "confirmed": cleanupErr == nil && readErr == nil && remaining == 0})
			if cleanupErr != nil || readErr != nil || remaining != 0 {
				t.Error("independent exact Agent Redis key cleanup failed")
			}
		})

		dsl := json.RawMessage(`{"components":{"begin":{"obj":{"component_name":"Begin","params":{}},"downstream":["message"],"upstream":[]},"message":{"obj":{"component_name":"Message","params":{"text":"Echo {{sys.query}}"}},"downstream":[],"upstream":["begin"]}}}`)
		title, updatedTitle := "owned Router Echo", "owned Router Echo saved"
		beforeCreate := after22SharedSQLFingerprint(t, env.Read)
		status, _ := call(t, "", http.MethodPost, "/api/v1/agents", service.SaveAgentRequest{Title: &title, DSL: dsl})
		if status != http.StatusUnauthorized || after22SharedSQLFingerprint(t, env.Read) != beforeCreate || len(hook.reservedKeys()) != 0 {
			t.Fatal("Agent creation bypassed actual JWT authentication")
		}
		status, raw := call(t, owner, http.MethodPost, "/api/v1/agents", service.SaveAgentRequest{Title: &title, DSL: dsl})
		var created struct {
			ID string `json:"id"`
		}
		success(t, status, raw, &created)
		if !after22SharedValidRunID(created.ID) {
			t.Fatal("public Agent create returned an invalid identity")
		}
		path := "/api/v1/agents/" + created.ID
		var savedTitle, savedOwner, savedDSL string
		if err := env.Read.QueryRow("SELECT title,user_id,dsl::text FROM t_ai_user_canvases WHERE id=$1", created.ID).Scan(&savedTitle, &savedOwner, &savedDSL); err != nil || savedTitle != title || savedOwner != owner {
			t.Fatal("public Agent create did not independently persist its owner/title")
		}
		var savedGraph map[string]any
		if json.Unmarshal([]byte(savedDSL), &savedGraph) != nil || savedGraph["components"] == nil {
			t.Fatal("public Agent create did not preserve executable DSL in SQL")
		}
		status, raw = call(t, owner, http.MethodPut, path, service.SaveAgentRequest{Title: &updatedTitle})
		success(t, status, raw, nil)
		if err := env.Read.QueryRow("SELECT title FROM t_ai_user_canvases WHERE id=$1 AND user_id=$2", created.ID, owner).Scan(&savedTitle); err != nil || savedTitle != updatedTitle {
			t.Fatal("public Agent update was not independently committed")
		}
		status, raw = call(t, owner, http.MethodGet, path, nil)
		var fetched struct {
			ID    string `json:"id"`
			Title string `json:"title"`
		}
		success(t, status, raw, &fetched)
		if fetched.ID != created.ID || fetched.Title != updatedTitle {
			t.Fatal("actual GET Agent route did not return the saved SQL row")
		}
		status, raw = call(t, owner, http.MethodPost, path+"/publish", nil)
		var published struct {
			ID string `json:"id"`
		}
		success(t, status, raw, &published)
		var publishedCanvas string
		var released bool
		if published.ID == "" {
			t.Fatal("public Agent publish omitted its persisted version")
		}
		if err := env.Read.QueryRow("SELECT user_canvas_id,release FROM t_ai_user_canvas_version WHERE id=$1", published.ID).Scan(&publishedCanvas, &released); err != nil || publishedCanvas != created.ID || !released {
			t.Fatal("public Agent publish did not independently persist its version")
		}

		beforeRun, modelCalls := after22SharedSQLFingerprint(t, env.Read), providerCalls.Load()
		status, raw = call(t, owner, http.MethodPost, path+"/run", service.AgentRunRequest{Query: "shared router", VersionID: published.ID})
		if status != http.StatusOK {
			t.Fatal("public Agent Echo run failed before SSE")
		}
		runID := ""
		started, messages, finished := 0, 0, 0
		for _, line := range strings.Split(string(raw), "\n") {
			if !strings.HasPrefix(line, "data: ") {
				continue
			}
			var event service.AgentRunEvent
			if json.Unmarshal([]byte(strings.TrimPrefix(line, "data: ")), &event) != nil || !after22SharedValidRunID(event.RunID) {
				t.Fatal("public Agent SSE has an invalid run envelope")
			}
			if runID == "" {
				runID = event.RunID
			} else if event.RunID != runID {
				t.Fatal("public Agent SSE changed run identity")
			}
			switch event.Event {
			case "workflow_started":
				started++
			case "message":
				messages++
				if event.Data["content"] != "Echo shared router" {
					t.Fatal("actual saved Echo canvas produced the wrong output")
				}
			case "workflow_finished":
				finished++
			case "error", "cancelled", "waiting_for_user":
				t.Fatalf("public Agent Echo returned unexpected terminal event %s", event.Event)
			}
		}
		keys := after22SharedRunKeys(runID)
		if runID == "" || started != 1 || messages != 1 || finished != 1 || !reflect.DeepEqual(hook.reservedKeys(), after22SharedSorted(keys)) {
			t.Fatal("public Agent Echo lacks a complete run or pre-write four-key reservation")
		}
		persisted, err := reader.Get(t.Context(), keys[0]).Bytes()
		var record struct {
			Status   string `json:"status"`
			ActorID  string `json:"actor_id"`
			CanvasID string `json:"canvas_id"`
			TenantID string `json:"tenant_id"`
		}
		if err != nil || json.Unmarshal(persisted, &record) != nil || record.Status != "succeeded" || record.ActorID != owner || record.CanvasID != created.ID || record.TenantID != owner {
			t.Fatal("independent Redis readback did not confirm the public Echo terminal state")
		}
		if n, err := reader.Exists(t.Context(), keys[1:]...).Result(); err != nil || n != 0 {
			t.Fatal("successful public Agent run retained cancel/lease/checkpoint resources")
		}
		if after22SharedSQLFingerprint(t, env.Read) != beforeRun || providerCalls.Load() != modelCalls {
			t.Fatal("Echo run changed saved SQL state or unexpectedly called a provider")
		}

		redisBefore, err := reader.MGet(t.Context(), keys...).Result()
		if err != nil {
			t.Fatal("independent pre-denial Redis readback")
		}
		for _, attempt := range []struct {
			method, suffix string
			payload        any
		}{
			{http.MethodGet, "", nil},
			{http.MethodPut, "", service.SaveAgentRequest{Title: &title}},
			{http.MethodDelete, "", nil},
			{http.MethodPost, "/publish", nil},
			{http.MethodDelete, "/versions/" + published.ID, nil},
			{http.MethodPost, "/run", service.AgentRunRequest{Query: "must not run"}},
			{http.MethodDelete, "/run", map[string]string{"run_id": runID}},
		} {
			status, raw = call(t, foreign, attempt.method, path+attempt.suffix, attempt.payload)
			var denial struct {
				Code *common.ErrorCode `json:"code"`
			}
			if status != http.StatusOK || json.Unmarshal(raw, &denial) != nil || denial.Code == nil || *denial.Code == common.CodeSuccess || bytes.Contains(raw, []byte("data: ")) {
				t.Fatalf("foreign JWT crossed Agent authorization at %s %s", attempt.method, attempt.suffix)
			}
		}
		redisAfter, err := reader.MGet(t.Context(), keys...).Result()
		if err != nil || !reflect.DeepEqual(redisBefore, redisAfter) || !reflect.DeepEqual(hook.reservedKeys(), after22SharedSorted(keys)) || after22SharedSQLFingerprint(t, env.Read) != beforeRun || providerCalls.Load() != modelCalls {
			t.Fatal("foreign Agent denial changed SQL/Redis or called a provider")
		}
		status, raw = call(t, owner, http.MethodDelete, path+"/run", map[string]string{"run_id": runID})
		success(t, status, raw, nil)
		afterCancel, err := reader.Get(t.Context(), keys[0]).Bytes()
		if err != nil || !bytes.Equal(persisted, afterCancel) {
			t.Fatal("cancel of terminal public run rewrote its succeeded state")
		}
		status, raw = call(t, owner, http.MethodDelete, path+"/versions/"+published.ID, nil)
		success(t, status, raw, nil)
		var remaining int
		if err := env.Read.QueryRow("SELECT count(*) FROM t_ai_user_canvas_version WHERE id=$1", published.ID).Scan(&remaining); err != nil || remaining != 0 {
			t.Fatal("public Agent version deletion was not independently committed")
		}
		status, raw = call(t, owner, http.MethodDelete, path, nil)
		success(t, status, raw, nil)
		if err := env.Read.QueryRow("SELECT count(*) FROM t_ai_user_canvases WHERE id=$1", created.ID).Scan(&remaining); err != nil || remaining != 0 {
			t.Fatal("public Agent deletion was not independently committed")
		}
		env.Journal.Event("shared-agent-public-echo-readback", map[string]any{"canvas_id": created.ID, "version_id": published.ID, "run_id": runID, "keys": keys, "actual_JWT": true, "actual_Router_Setup": true, "actual_Redis_terminal": "succeeded", "foreign_SQL_Redis_unchanged": true, "model_called": false})
	})
}

func after22SharedID() string { return strings.ReplaceAll(uuid.NewString(), "-", "") }

// Read only known tables inside scratch.New's exclusively owned database.
// Fingerprints retain evidence of updates without logging stored credentials.
func after22SharedSQLFingerprint(t testing.TB, reader *sql.DB) [32]byte {
	t.Helper()
	var state strings.Builder
	for _, table := range []string{"t_ai_users", "t_ai_tenants", "t_ai_user_tenants", "t_ai_tenant_llms", "tenant_model_provider", "tenant_model_instance", "tenant_model", "t_ai_files", "t_ai_file_commits", "t_ai_file_commit_items", "t_ai_user_canvases", "t_ai_user_canvas_version"} {
		var rows string
		query := "SELECT COALESCE(jsonb_agg(to_jsonb(r) ORDER BY to_jsonb(r)::text)::text,'[]') FROM " + table + " r"
		if err := reader.QueryRowContext(t.Context(), query).Scan(&rows); err != nil {
			t.Fatalf("independent SQL fingerprint failed for %s", table)
		}
		state.WriteString(table)
		state.WriteByte('\n')
		state.WriteString(rows)
		state.WriteByte('\n')
	}
	return sha256.Sum256([]byte(state.String()))
}

func after22SharedValidRunID(id string) bool {
	if len(id) != 32 || strings.ToLower(id) != id {
		return false
	}
	_, err := hex.DecodeString(id)
	return err == nil
}

func after22SharedRunKeys(id string) []string {
	return []string{"agent:execution:" + id, "agent:execution:" + id + ":cancel", "agent:execution:" + id + ":lock", "agent:cp:" + id}
}

func after22SharedSorted(values []string) []string {
	result := append([]string(nil), values...)
	sort.Strings(result)
	return result
}

// This hook forwards every command unchanged. It only reserves all four run
// resources before the production service's lease SET ... [PX|EX ttl] NX.
type after22SharedRunJournal struct {
	mu      sync.Mutex
	journal *testsupport.CaseJournal
	reader  *rawredis.Client
	keys    map[string]bool
}

func (h *after22SharedRunJournal) DialHook(next rawredis.DialHook) rawredis.DialHook {
	return next
}

func (h *after22SharedRunJournal) ProcessHook(next rawredis.ProcessHook) rawredis.ProcessHook {
	return func(ctx context.Context, command rawredis.Cmder) error {
		if err := h.reserve(ctx, command); err != nil {
			return err
		}
		return next(ctx, command)
	}
}

func (h *after22SharedRunJournal) ProcessPipelineHook(next rawredis.ProcessPipelineHook) rawredis.ProcessPipelineHook {
	return func(ctx context.Context, commands []rawredis.Cmder) error {
		for _, command := range commands {
			if err := h.reserve(ctx, command); err != nil {
				return err
			}
		}
		return next(ctx, commands)
	}
}

func (h *after22SharedRunJournal) reserve(ctx context.Context, command rawredis.Cmder) error {
	args := command.Args()
	if len(args) < 3 || (strings.ToLower(command.Name()) != "set" && strings.ToLower(command.Name()) != "setnx") {
		return nil
	}
	key, ok := args[1].(string)
	if !ok || (!strings.HasPrefix(key, "agent:execution:") && !strings.HasPrefix(key, "agent:cp:")) {
		return nil
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.keys[key] {
		return nil
	}
	claimedKey, claims := after22SharedLockClaim(command)
	if !claims || claimedKey != key || !strings.HasPrefix(key, "agent:execution:") || !strings.HasSuffix(key, ":lock") {
		return fmt.Errorf("public Agent attempted an unreserved Redis write")
	}
	id := strings.TrimSuffix(strings.TrimPrefix(key, "agent:execution:"), ":lock")
	if !after22SharedValidRunID(id) {
		return fmt.Errorf("public Agent attempted an invalid run identity")
	}
	keys := after22SharedRunKeys(id)
	h.journal.Event("reserve-shared-agent-run-before-redis-write", map[string]any{"run_id": id, "keys": keys})
	count, err := h.reader.Exists(ctx, keys...).Result()
	if err != nil || count != 0 {
		// Existing keys are never adopted or cleaned. The command has not reached
		// next, so a collision cannot become a write to an unknown resource.
		return fmt.Errorf("public Agent exact Redis key absence precondition failed")
	}
	for _, reserved := range keys {
		h.keys[reserved] = true
	}
	return nil
}

func (h *after22SharedRunJournal) reservedKeys() []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	keys := make([]string, 0, len(h.keys))
	for key := range h.keys {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

// Parse options, never the value or an expiry operand, when finding NX. Real
// go-redis SetNX with a TTL is encoded as SET rather than the SETNX command.
func after22SharedLockClaim(command rawredis.Cmder) (string, bool) {
	args := command.Args()
	if len(args) < 3 {
		return "", false
	}
	key, ok := args[1].(string)
	if !ok {
		return "", false
	}
	switch strings.ToLower(command.Name()) {
	case "setnx":
		return key, len(args) == 3
	case "set":
		nx := false
		for i := 3; i < len(args); i++ {
			var option string
			switch value := args[i].(type) {
			case string:
				option = value
			case []byte:
				option = string(value)
			default:
				return "", false
			}
			switch strings.ToUpper(option) {
			case "NX":
				if nx {
					return "", false
				}
				nx = true
			case "EX", "PX", "EXAT", "PXAT":
				if i+1 >= len(args) {
					return "", false
				}
				i++
			case "KEEPTTL", "GET":
			default:
				return "", false
			}
		}
		return key, nx
	default:
		return "", false
	}
}
