package router_test

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
	"gorm.io/gorm"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/testsupport"
	"multirag/internal/utility"
)

// This fixture acknowledges that neither external store exists. Unexpected
// writes cannot succeed through the embedded nil interface.
type afterFFAbsentDocumentIndex struct {
	engine.DocEngine
	tenant, dataset string
}

func (s *afterFFAbsentDocumentIndex) ChunkStoreExists(ctx context.Context, base, dataset string) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, err
	}
	if base != "multirag_"+s.tenant || dataset != s.dataset {
		return false, errors.New("wrong document index scope")
	}
	return false, nil
}
func (s *afterFFAbsentDocumentIndex) MetadataStoreExists(ctx context.Context, tenant string) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, err
	}
	if tenant != s.tenant {
		return false, errors.New("wrong metadata scope")
	}
	return false, nil
}

// TestAfterFFDocumentsJWTHTTPAndSQL exercises the actual registered DELETE route
// with signed users, PostgreSQL, and independently observed Redis lease state.
// Source-less documents keep this test focused on route and authority contracts;
// storage and worker failure paths have their own lifecycle tests.
func TestAfterFFDocumentsJWTHTTPAndSQL(t *testing.T) {
	journal := testsupport.NewCase(t)
	config := os.Getenv("MULTIRAG_AFTERFF_G_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned afterff G PostgreSQL and Redis config required")
		}
		t.Skip("requires owned afterff G config")
	}
	address := "127.0.0.1:43853"
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal("owned config unavailable")
	}
	if len(server.GetConfig().Server.SecretKey) < 32 {
		t.Fatal("owned fixture requires a configured signing key")
	}
	// The configured key initializes process-local runtime variables without
	// touching the shared Redis system secret-key namespace.
	if err := server.InitVariables(nil); err != nil {
		t.Fatal("fixture runtime signing variables unavailable")
	}
	cfg := server.GetConfig()
	if cfg.Database.Host != "127.0.0.1" || cfg.Database.Port != 5432 || cfg.Database.Database != "multirag_batch_g_afterff_20261028" || cfg.Database.Schema != "usr_ai" {
		t.Fatal("exact owned PostgreSQL target required")
	}
	journal.Event("reserve-schema-before-SQL", map[string]any{"database": cfg.Database.Database, "schema": cfg.Database.Schema, "host": cfg.Database.Host, "port": cfg.Database.Port})
	if err := dao.InitDB(); err != nil {
		t.Fatal("owned schema initialization failed")
	}
	pool, err := dao.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = pool.Close() })
	dsn, err := dao.PostgresDSN(cfg.Database)
	if err != nil {
		t.Fatal(err)
	}
	read, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("independent SQL connection failed")
	}
	t.Cleanup(func() { _ = read.Close() })
	if err := read.PingContext(context.Background()); err != nil {
		t.Fatal("independent SQL unavailable")
	}
	redisAddress := net.JoinHostPort(cfg.Redis.Host, fmt.Sprint(cfg.Redis.Port))
	redisOptions := &redis.Options{Addr: redisAddress, Password: cfg.Redis.Password, DB: cfg.Redis.DB, DialTimeout: 3 * time.Second, ReadTimeout: 3 * time.Second, WriteTimeout: 3 * time.Second}
	redisClient := redis.NewClient(redisOptions)
	t.Cleanup(func() { _ = redisClient.Close() })
	redisRead := redis.NewClient(redisOptions)
	t.Cleanup(func() { _ = redisRead.Close() })
	if err := redisClient.Ping(context.Background()).Err(); err != nil {
		t.Fatal("borrowed Redis unavailable")
	}
	if err := redisRead.Ping(context.Background()).Err(); err != nil {
		t.Fatal("independent Redis unavailable")
	}

	suffix := os.Getenv("MULTIRAG_RUN_ID")
	if len(suffix) < 12 {
		suffix = strings.ReplaceAll(uuid.NewString(), "-", "")
	}
	suffix = suffix[:12]
	owner, other, member, inactive := "gfd-own-"+suffix, "gfd-oth-"+suffix, "gfd-mem-"+suffix, "gfd-off-"+suffix
	kb, otherKB := "gfd-kb-"+suffix, "gfd-okb-"+suffix
	a, b, foreign := "gfd-a-"+suffix, "gfd-b-"+suffix, "gfd-x-"+suffix
	leaseKey := "graphrag:writer:" + owner + ":" + kb
	journal.Event("reserve-Redis-key", map[string]any{"address": redisAddress, "db": cfg.Redis.DB, "key": leaseKey})
	if exists, err := redisRead.Exists(context.Background(), leaseKey).Result(); err != nil || exists != 0 {
		t.Fatal("owned graph lease key was not initially absent")
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		before, err := redisRead.Exists(ctx, leaseKey).Result()
		if err != nil {
			t.Error("Redis cleanup state unavailable")
		} else if before != 0 {
			t.Error("graph lease survived request completion")
			if err := redisClient.Del(ctx, leaseKey).Err(); err != nil {
				t.Error("owned graph lease recovery failed")
			}
		}
		remaining, err := redisRead.Exists(ctx, leaseKey).Result()
		journal.Event("cleanup-Redis-key-readback", map[string]any{"address": redisAddress, "db": cfg.Redis.DB, "key": leaseKey, "remaining": remaining, "confirmed": err == nil && remaining == 0})
		if err != nil || remaining != 0 {
			t.Error("owned graph lease remains")
		}
	})
	callbackName := "afterff_document_resources_" + suffix
	rowIDs := map[string][]string{}
	if err := dao.DB.Callback().Create().Before("gorm:create").Register(callbackName, func(tx *gorm.DB) {
		value := tx.Statement.ReflectValue
		if value.Kind() == reflect.Pointer {
			value = value.Elem()
		}
		var ids []string
		appendID := func(v reflect.Value) {
			if v.Kind() == reflect.Pointer {
				v = v.Elem()
			}
			if v.Kind() == reflect.Struct {
				field := v.FieldByName("ID")
				if field.IsValid() {
					ids = append(ids, fmt.Sprint(field.Interface()))
				}
			}
		}
		if value.Kind() == reflect.Slice || value.Kind() == reflect.Array {
			for i := 0; i < value.Len(); i++ {
				appendID(value.Index(i))
			}
		} else {
			appendID(value)
		}
		journal.Event("reserve-rows-before-SQL", map[string]any{"table": tx.Statement.Table, "ids": ids})
		rowIDs[tx.Statement.Table] = append(rowIDs[tx.Statement.Table], ids...)
	}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = dao.DB.Callback().Create().Remove(callbackName) })
	t.Cleanup(func() {
		for _, table := range []string{"t_ai_documents", "t_ai_knowledgebases", "t_ai_user_tenants", "t_ai_tenants", "t_ai_users"} {
			ids := rowIDs[table]
			if len(ids) == 0 {
				continue
			}
			marks := make([]string, len(ids))
			args := make([]any, len(ids))
			for i, id := range ids {
				marks[i] = fmt.Sprintf("$%d", i+1)
				args[i] = id
			}
			condition := "id IN (" + strings.Join(marks, ",") + ")"
			if _, err := read.Exec("DELETE FROM "+table+" WHERE "+condition, args...); err != nil {
				t.Error("owned document route SQL cleanup failed")
			}
			var remaining int
			err := read.QueryRow("SELECT count(*) FROM "+table+" WHERE "+condition, args...).Scan(&remaining)
			journal.Event("cleanup-document-route-SQL-readback", map[string]any{"table": table, "ids": ids, "remaining": remaining, "confirmed": err == nil && remaining == 0})
			if err != nil || remaining != 0 {
				t.Error("owned document route SQL rows remain")
			}
		}
	})
	mustCreate := func(value any) {
		t.Helper()
		if err := dao.DB.Create(value).Error; err != nil {
			state := "unavailable"
			var sqlState interface{ SQLState() string }
			if errors.As(err, &sqlState) {
				state = sqlState.SQLState()
			}
			t.Fatalf("owned row creation failed: row_type=%T error_type=%T SQLSTATE=%s", value, err, state)
		}
	}
	active, disabled := "1", "0"
	for _, id := range []string{owner, other, member, inactive} {
		status := active
		if id == inactive {
			status = disabled
		}
		mustCreate(&entity.User{ID: id, Email: id + "@example.invalid", Nickname: id, Status: &status, IsActive: true, IsAuthenticated: true})
		mustCreate(&entity.Tenant{ID: id, Status: &active})
	}
	for _, membership := range []entity.UserTenant{
		{ID: "gfd-om-" + suffix, UserID: owner, TenantID: owner, InvitedBy: owner, Role: "owner", Status: &active},
		{ID: "gfd-otm-" + suffix, UserID: other, TenantID: other, InvitedBy: other, Role: "owner", Status: &active},
		{ID: "gfd-mm-" + suffix, UserID: member, TenantID: owner, InvitedBy: owner, Role: "normal", Status: &active},
	} {
		row := membership
		mustCreate(&row)
	}
	mustCreate(&entity.Knowledgebase{ID: kb, TenantID: owner, CreatedBy: owner, Name: "afterff-http-owned", DocNum: 2, ParserConfig: entity.JSONMap{}, Status: &active})
	mustCreate(&entity.Knowledgebase{ID: otherKB, TenantID: other, CreatedBy: other, Name: "afterff-http-foreign", DocNum: 1, ParserConfig: entity.JSONMap{}, Status: &active})
	for _, id := range []string{a, b} {
		mustCreate(&entity.Document{ID: id, KbID: kb, CreatedBy: owner, ParserID: "naive", ParserConfig: entity.JSONMap{}, MetaFields: &entity.JSONMap{}, SourceType: "local", Type: "txt", Status: &active})
	}
	mustCreate(&entity.Document{ID: foreign, KbID: otherKB, CreatedBy: other, ParserID: "naive", ParserConfig: entity.JSONMap{}, MetaFields: &entity.JSONMap{}, SourceType: "local", Type: "txt", Status: &active})

	snapshot := func() string {
		t.Helper()
		var parts []string
		for _, table := range []string{"t_ai_users", "t_ai_tenants", "t_ai_user_tenants", "t_ai_knowledgebases", "t_ai_documents", "t_ai_tasks", "t_ai_files", "t_ai_file2documents"} {
			var rows string
			if err := read.QueryRow("SELECT COALESCE(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text)::text,'[]') FROM " + table + " x").Scan(&rows); err != nil {
				t.Fatal("independent document route snapshot failed")
			}
			parts = append(parts, rows)
		}
		return fmt.Sprintf("%x", sha256.Sum256([]byte(strings.Join(parts, "\n"))))
	}
	assertNoLease := func() {
		t.Helper()
		exists, err := redisRead.Exists(context.Background(), leaseKey).Result()
		if err != nil || exists != 0 {
			t.Fatal("request left a graph lease")
		}
	}
	oldAdmin := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned afterff document fixture")
	t.Cleanup(func() { local.SetAdminStatus(oldAdmin.Status, oldAdmin.Reason) })
	index := &afterFFAbsentDocumentIndex{tenant: owner, dataset: kb}
	documents := service.NewDocumentService(index)
	documents.SetDocumentDeleteService(service.NewDocumentDeleteService(dao.DB, redisClient, index, nil, server.EngineElasticsearch))
	app := gin.New()
	router.NewRouter(handler.NewAuthHandler(), nil, nil, handler.NewDocumentHandler(documents), nil, handler.NewSystemHandler(service.NewSystemService()), nil, nil, nil, nil, nil, nil, nil, nil, nil, nil).Setup(app)
	found := false
	for _, route := range app.Routes() {
		if route.Method == http.MethodDelete && route.Path == "/api/v1/documents/:id" {
			t.Fatal("retired single-document deletion route remains")
		}
		if route.Method == http.MethodDelete && route.Path == "/api/v1/datasets/:dataset_id/documents" {
			found = true
		}
	}
	if !found {
		t.Fatal("dataset document deletion route missing")
	}
	journal.Event("reserve-listener", map[string]any{"address": address})
	listener, err := net.Listen("tcp", address)
	if err != nil {
		t.Fatal("exact document fixture listener unavailable")
	}
	live := &httptest.Server{Listener: listener, Config: &http.Server{Handler: app}}
	live.Start()
	t.Cleanup(func() {
		live.Close()
		connection, err := net.DialTimeout("tcp", address, 150*time.Millisecond)
		if connection != nil {
			_ = connection.Close()
		}
		journal.Event("cleanup-listener-readback", map[string]any{"address": address, "absent": err != nil})
		if err == nil {
			t.Error("owned document HTTP listener remains")
		}
	})
	transport := http.DefaultTransport.(*http.Transport).Clone()
	t.Cleanup(transport.CloseIdleConnections)
	client := &http.Client{Transport: transport, Timeout: 8 * time.Second}
	tokens := map[string]string{}
	for _, id := range []string{owner, other, member, inactive} {
		token, err := utility.SignUserJWT(id+"@example.invalid", server.GetSecretKey())
		if err != nil {
			t.Fatal("fixture JWT signing failed")
		}
		tokens[id] = token
	}
	type responseEnvelope struct {
		Code *common.ErrorCode `json:"code"`
		Data *struct {
			Deleted int `json:"deleted"`
		} `json:"data"`
	}
	request := func(actor, body string) (int, responseEnvelope) {
		t.Helper()
		req, err := http.NewRequest(http.MethodDelete, live.URL+"/api/v1/datasets/"+kb+"/documents", strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		if actor == "invalid-token" {
			req.Header.Set("Authorization", "Bearer invalid-token")
		} else if actor != "" {
			req.Header.Set("Authorization", "Bearer "+tokens[actor])
		}
		response, err := client.Do(req)
		if err != nil {
			t.Fatal("document HTTP request failed")
		}
		raw, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
		_ = response.Body.Close()
		if err != nil {
			t.Fatal("document HTTP response read failed")
		}
		var result responseEnvelope
		if json.Unmarshal(raw, &result) != nil || result.Code == nil {
			t.Fatal("document response envelope invalid")
		}
		return response.StatusCode, result
	}
	for _, actor := range []string{"", "invalid-token", inactive} {
		before := snapshot()
		status, _ := request(actor, `{"ids":["`+a+`"]}`)
		if status != http.StatusUnauthorized || snapshot() != before {
			t.Fatal("unauthenticated document deletion changed SQL")
		}
		assertNoLease()
	}
	denied := func(actor, body string, want common.ErrorCode) {
		t.Helper()
		before := snapshot()
		status, result := request(actor, body)
		if status != http.StatusOK || *result.Code != want || snapshot() != before {
			t.Fatal("rejected document selection changed SQL or returned the wrong business code")
		}
		assertNoLease()
	}
	for _, actor := range []string{other, member} {
		denied(actor, `{"ids":["`+a+`"]}`, common.CodeForbidden)
		denied(actor, `{"ids":[]}`, common.CodeForbidden)
	}
	for _, body := range []string{"", `{}`, `null`, `[]`, `{"ids":null}`, `{"ids":true}`, `{"ids":[true]}`, `{"ids":"wrong"}`, `{"ids":[""]}`, `{"ids":[" "]}`, `{"delete_all":false}`, `{"delete_all":null}`, `{"delete_all":"true"}`, `{"ids":[],"delete_all":true}`, `{"ids":["` + a + `"],"delete_all":true}`} {
		denied(owner, body, common.CodeBadRequest)
	}
	denied(owner, `{"ids":["unknown-`+suffix+`"]}`, common.CodeForbidden)
	denied(owner, `{"ids":["`+a+`","`+foreign+`"]}`, common.CodeForbidden)
	before := snapshot()
	status, result := request(owner, `{"ids":[]}`)
	if status != http.StatusOK || *result.Code != common.CodeSuccess || result.Data == nil || result.Data.Deleted != 0 || snapshot() != before {
		t.Fatal("explicit empty ids broadened deletion or skipped authorization")
	}
	assertNoLease()
	status, result = request(owner, `{"ids":["`+a+`","`+a+`"]}`)
	if status != http.StatusOK || *result.Code != common.CodeSuccess || result.Data == nil || result.Data.Deleted != 1 {
		t.Fatal("duplicate document ids were not deleted once")
	}
	assertNoLease()
	var count, docNum int
	if err := read.QueryRow("SELECT count(*) FROM t_ai_documents WHERE id=$1", a).Scan(&count); err != nil || count != 0 {
		t.Fatal("deleted document remains")
	}
	if err := read.QueryRow("SELECT doc_num FROM t_ai_knowledgebases WHERE id=$1", kb).Scan(&docNum); err != nil || docNum != 1 {
		t.Fatal("dataset document count did not decrement once")
	}
	if err := read.QueryRow("SELECT count(*) FROM t_ai_documents WHERE id=$1 AND kb_id=$2", b, kb).Scan(&count); err != nil || count != 1 {
		t.Fatal("unselected document changed")
	}
	status, result = request(owner, `{"delete_all":true}`)
	if status != http.StatusOK || *result.Code != common.CodeSuccess || result.Data == nil || result.Data.Deleted != 1 {
		t.Fatal("delete_all did not delete the complete remaining selection")
	}
	assertNoLease()
	if err := read.QueryRow("SELECT count(*) FROM t_ai_documents WHERE kb_id=$1", kb).Scan(&count); err != nil || count != 0 {
		t.Fatal("delete_all left dataset documents")
	}
	if err := read.QueryRow("SELECT doc_num FROM t_ai_knowledgebases WHERE id=$1", kb).Scan(&docNum); err != nil || docNum != 0 {
		t.Fatal("delete_all count did not reach zero")
	}
	if err := read.QueryRow("SELECT count(*) FROM t_ai_documents WHERE id=$1 AND kb_id=$2", foreign, otherKB).Scan(&count); err != nil || count != 1 {
		t.Fatal("foreign document changed")
	}
	if err := read.QueryRow("SELECT doc_num FROM t_ai_knowledgebases WHERE id=$1", otherKB).Scan(&docNum); err != nil || docNum != 1 {
		t.Fatal("foreign dataset count changed")
	}
	before = snapshot()
	status, result = request(owner, `{"delete_all":true}`)
	if status != http.StatusOK || *result.Code != common.CodeSuccess || result.Data == nil || result.Data.Deleted != 0 || snapshot() != before {
		t.Fatal("empty delete_all was not a deterministic no-op")
	}
	assertNoLease()
	journal.Event("document-route-contract-readback", map[string]any{"dataset_id": kb, "deleted_ids": []string{a, b}, "foreign_document": foreign, "foreign_dataset": otherKB, "explicit_empty": 0, "duplicate_selection_deleted": 1, "delete_all_deleted": 1, "foreign_preserved": true, "lease_key": leaseKey, "lease_absent": true, "external_store_absence_controlled": true})
}
