//go:build integration

package handler_test

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/testsupport"
	"multirag/internal/utility"
)

// Run explicitly with -tags integration and MULTIRAG_AFTER10BBE_G_CONFIG.
// Missing services fail; this test never substitutes a live skip for acceptance.
func TestAfter10bbeAgentMetadataHTTPAndSQL(t *testing.T) {
	path := os.Getenv("MULTIRAG_AFTER10BBE_G_CONFIG")
	if path == "" || os.Getenv("REQUIRE_SERVICES") != "1" {
		t.Fatal("registered scratch config and REQUIRE_SERVICES=1 required")
	}
	j := testsupport.NewCase(t)
	var cfg struct {
		AdminDSN   string `json:"admin_dsn"`
		Listen     string `json:"listen"`
		PGEndpoint string `json:"pg_endpoint"`
	}
	raw, err := os.ReadFile(path)
	if err != nil || json.Unmarshal(raw, &cfg) != nil {
		t.Fatal("private config unavailable")
	}
	u, err := url.Parse(cfg.AdminDSN)
	if err != nil || u.Host != cfg.PGEndpoint || u.Hostname() != "127.0.0.1" || u.Path != "/postgres" || cfg.Listen != "127.0.0.1:18961" {
		t.Fatal("exact local endpoints required")
	}
	port, err := strconv.Atoi(u.Port())
	if err != nil || port < 1 || port > 65535 {
		t.Fatal("explicit local PostgreSQL TCP port required")
	}
	name := "multirag_after10bbe_g_" + strings.ReplaceAll(uuid.NewString(), "-", "")[:16]
	tables := []string{"t_ai_users", "t_ai_tenants", "t_ai_user_tenants", "t_ai_api_tokens", "t_ai_user_canvases", "t_ai_api4conversations", "t_ai_knowledgebases", "t_ai_documents"}
	j.Event("reserve-connections-and-database-before-create", map[string]any{"endpoint": u.Host, "database": name, "schema": "public", "tables": tables, "config": path, "pools": []string{"admin", "gorm", "independent-reader"}})
	admin, err := sql.Open("pgx", cfg.AdminDSN)
	if err != nil {
		t.Fatal("admin open")
	}
	t.Cleanup(func() { admin.Close() })
	var exists bool
	if err = admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&exists); err != nil || exists {
		t.Fatal("database absence precondition")
	}
	t.Cleanup(func() {
		_, dropErr := admin.Exec(`DROP DATABASE IF EXISTS "` + name + `" WITH (FORCE)`)
		readErr := admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&exists)
		j.Event("database-cleanup-independent-readback", map[string]any{"database": name, "absent": readErr == nil && !exists, "drop_ok": dropErr == nil})
		if dropErr != nil || readErr != nil || exists {
			t.Error("database survived cleanup")
		}
	})
	if _, err = admin.Exec(`CREATE DATABASE "` + name + `"`); err != nil {
		t.Fatal("scratch database create")
	}
	u.Path = "/" + name
	db, err := gorm.Open(postgres.Open(u.String()), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("scratch SQL connect")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	read, err := sql.Open("pgx", u.String())
	if err != nil {
		t.Fatal("independent reader")
	}
	t.Cleanup(func() { read.Close() })
	beforeDB := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = beforeDB })
	if err = db.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.APIToken{}, &entity.UserCanvas{}, &entity.API4Conversation{}, &entity.Knowledgebase{}, &entity.Document{}); err != nil {
		t.Fatal(err)
	}
	oldKey := server.GetSecretKey()
	if err = server.FromConfigFile(path); err != nil {
		t.Fatal("private signing config")
	}
	if err = server.InitVariables(nil); err != nil {
		t.Fatal("signing setup")
	}
	t.Cleanup(func() { server.SetSecretKey(oldKey) })
	license := local.GetAdminStatus()
	local.SetAdminStatus(0, "controlled after10bbe G")
	t.Cleanup(func() { local.SetAdminStatus(license.Status, license.Reason) })
	if err = common.Init("error"); err != nil {
		t.Fatal(err)
	}
	must := func(v any) {
		t.Helper()
		if err := db.Create(v).Error; err != nil {
			t.Fatal(err)
		}
	}
	active, inactive, access, agent, chat := "1", "0", "fixture-access", "agent", "chat"
	principals := []string{"owner", "member", "manager", "invite", "revoked", "foreign", "disabled"}
	kb, otherKB := "00000000000010008000000000000001", "00000000000010008000000000000002"
	j.Event("reserve-fixture-identities-before-insert", map[string]any{"users": principals, "tenants": []string{"owner", "foreign", "disabled"}, "canvases": []string{"shared", "other", "disabled"}, "sessions": []string{"s-a", "s-b", "s-chat", "s-other", "s-disabled"}, "datasets": []string{kb, otherKB}, "documents": []string{"doc", "doc-other", "doc-disabled"}, "token_aliases": []string{"owner-key", "foreign-key", "disabled-key", "ambiguous-key"}})
	for _, id := range principals {
		must(&entity.User{ID: id, Email: id + "@after10bbe.invalid", Nickname: id, Status: &active, AccessToken: &access})
	}
	if err = db.Model(&entity.User{}).Where("id=?", "disabled").Update("is_active", false).Error; err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"owner", "foreign", "disabled"} {
		must(&entity.Tenant{ID: id, Status: &active})
	}
	for _, row := range []struct{ id, role, status string }{{"owner", "owner", active}, {"member", "normal", active}, {"manager", "admin", active}, {"invite", "invite", active}, {"revoked", "admin", inactive}, {"foreign", "owner", active}} {
		tenant := "owner"
		if row.id == "foreign" {
			tenant = "foreign"
		}
		must(&entity.UserTenant{ID: row.id, UserID: row.id, TenantID: tenant, Role: row.role, Status: &row.status})
	}
	for _, canvas := range []entity.UserCanvas{{ID: "shared", UserID: "owner", Permission: "team"}, {ID: "other", UserID: "foreign", Permission: "me"}, {ID: "disabled", UserID: "disabled", Permission: "team"}} {
		must(&canvas)
	}
	for _, id := range []string{"owner", "foreign", "disabled"} {
		must(&entity.APIToken{TenantID: id, Token: id + "-key"})
	}
	for _, tenant := range []string{"owner", "foreign"} {
		must(&entity.APIToken{TenantID: tenant, Token: "ambiguous-key"})
	}
	for _, row := range []struct {
		id, parent, user string
		source           *string
	}{{"s-a", "shared", "foreign", &agent}, {"s-b", "shared", "owner", &agent}, {"s-chat", "shared", "owner", &chat}, {"s-other", "other", "foreign", &agent}, {"s-disabled", "disabled", "owner", &agent}} {
		must(&entity.API4Conversation{ID: row.id, DialogID: row.parent, UserID: row.user, Source: row.source, Message: json.RawMessage(`[]`), Reference: json.RawMessage(`[]`)})
	}
	for _, row := range []struct{ id, tenant string }{{kb, "owner"}, {otherKB, "foreign"}} {
		must(&entity.Knowledgebase{ID: row.id, TenantID: row.tenant, CreatedBy: row.tenant, Name: row.id, Permission: "team", Status: &active, ParserConfig: entity.JSONMap{}})
	}
	for _, row := range []struct{ id, kb, status string }{{"doc", kb, active}, {"doc-other", otherKB, active}, {"doc-disabled", kb, inactive}} {
		must(&entity.Document{ID: row.id, KbID: row.kb, CreatedBy: "owner", Status: &row.status, ParserConfig: entity.JSONMap{}})
	}
	if _, err = read.Exec(`UPDATE t_ai_documents SET parser_config=$1::jsonb WHERE id='doc'`, `{"model_id":9007199254740993,"zero":0,"enabled":false,"metadata":{"properties":{"old":{}}}}`); err != nil {
		t.Fatal(err)
	}
	snapshot := func(table string) string {
		t.Helper()
		var raw string
		if err := read.QueryRow("SELECT COALESCE(json_agg(s ORDER BY id)::text,'[]') FROM " + table + " s").Scan(&raw); err != nil {
			t.Fatal(err)
		}
		return raw
	}
	gin.SetMode(gin.TestMode)
	routes := gin.New()
	router.NewRouter(handler.NewAuthHandler(), nil, nil, nil, handler.NewDatasetsHandler(service.NewDatasetsService()), nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil).Setup(routes)
	j.Event("reserve-listener-before-listen", map[string]any{"address": cfg.Listen, "network": "tcp4", "pid": os.Getpid()})
	listener, err := net.Listen("tcp4", cfg.Listen)
	if err != nil {
		t.Fatal(err)
	}
	srv := &http.Server{Handler: routes, ReadHeaderTimeout: time.Second}
	done := make(chan error, 1)
	go func() { done <- srv.Serve(listener) }()
	t.Cleanup(func() {
		srv.Close()
		serveErr := <-done
		conn, err := net.DialTimeout("tcp4", cfg.Listen, time.Second)
		if conn != nil {
			conn.Close()
		}
		absent := err != nil
		j.Event("listener-cleanup-readback", map[string]any{"address": cfg.Listen, "absent": absent})
		if !errors.Is(serveErr, http.ErrServerClosed) || !absent {
			t.Error("listener cleanup")
		}
	})
	client := &http.Client{Timeout: 5 * time.Second}
	t.Cleanup(client.CloseIdleConnections)
	request := func(method, path, actor, body string, want common.ErrorCode) map[string]any {
		t.Helper()
		req, err := http.NewRequest(method, "http://"+cfg.Listen+path, strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		if actor != "" {
			token := actor
			if !strings.HasSuffix(actor, "-key") {
				token, err = utility.SignUserJWT(actor+"@after10bbe.invalid", server.GetSecretKey())
				if err != nil {
					t.Fatal(err)
				}
			}
			req.Header.Set("Authorization", "Bearer "+token)
		}
		req.Header.Set("Content-Type", "application/json")
		resp, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		raw, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatal(err)
		}
		decoder := json.NewDecoder(strings.NewReader(string(raw)))
		decoder.UseNumber()
		var result map[string]any
		if decoder.Decode(&result) != nil || result["code"] != json.Number(strconv.Itoa(int(want))) {
			t.Fatalf("%s %s actor=%s: status=%d response=%s want=%d", method, path, actor, resp.StatusCode, raw, want)
		}
		if strings.Contains(string(raw), "private-sql-marker") {
			t.Fatal("SQL error leaked")
		}
		return result
	}
	sessions := "/api/v1/agents/shared/sessions"
	metadata := "/api/v1/datasets/" + kb + "/documents/doc/metadata/config"
	probe := "/api/v1/agents/test_db_connection"
	beforeSessions, beforeDocs := snapshot("t_ai_api4conversations"), snapshot("t_ai_documents")
	for _, r := range []struct{ method, path, payload string }{{"DELETE", sessions, `{"delete_all":true}`}, {"PUT", metadata, `{"metadata":[]}`}, {"POST", probe, `{}`}} {
		request(r.method, r.path, "", r.payload, common.CodeUnauthorized)
	}
	for _, actor := range []string{"member", "manager", "invite", "revoked", "foreign", "foreign-key"} {
		request("DELETE", sessions, actor, `{"delete_all":true}`, common.CodeOperatingError)
	}
	request("DELETE", sessions, "disabled-key", `{"delete_all":true}`, common.CodeUnauthorized)
	request("DELETE", sessions, "ambiguous-key", `{"delete_all":true}`, common.CodeUnauthorized)
	for _, payload := range []string{`{}`, `{"delete_all":false}`, `{"ids":[]}`, `{"ids":[],"delete_all":true}`} {
		request("DELETE", sessions, "owner", payload, common.CodeSuccess)
	}
	for _, payload := range []string{`{"ids":null}`, `{"delete_all":"true"}`, `{"delete_all":null}`, `{"ids":[null]}`, `{"ids":[],"ids":["s-a"]}`, `{"ids":[],"unknown":true}`, `{} {}`} {
		request("DELETE", sessions, "owner", payload, common.CodeArgumentError)
	}
	var deniedDocumentReads atomic.Int32
	if err = db.Callback().Query().Before("gorm:query").Register("after10bbe-g-denied-read", func(tx *gorm.DB) {
		if tx.Statement.Table == "t_ai_documents" {
			deniedDocumentReads.Add(1)
		}
	}); err != nil {
		t.Fatal(err)
	}
	for _, actor := range []string{"member", "invite", "revoked", "foreign", "foreign-key"} {
		request("PUT", metadata, actor, `{"metadata":[]}`, common.CodeForbidden)
	}
	if deniedDocumentReads.Load() != 0 {
		t.Fatal("authorization read document before denial")
	}
	if err = db.Callback().Query().Remove("after10bbe-g-denied-read"); err != nil {
		t.Fatal(err)
	}
	request("PUT", "/api/v1/datasets/"+kb+"/documents/doc-other/metadata/config", "owner", `{"metadata":[]}`, common.CodeNotFound)
	request("PUT", "/api/v1/datasets/"+kb+"/documents/doc-disabled/metadata/config", "owner", `{"metadata":[]}`, common.CodeNotFound)
	for _, payload := range []string{`{}`, `{"metadata":null}`, `{"metadata":1}`, `{"metadata":[],"unknown":true}`, `{"metadata":[],"metadata":{}}`, `{"metadata":[]} {}`} {
		request("PUT", metadata, "owner", payload, common.CodeArgumentError)
	}
	for _, payload := range []string{`{"db_type":"mysql","database":"fixture","username":"user","password":"fixture","host":"127.0.0.1","port":3306}`, `{"db_type":"mysql","database":"fixture","username":"user","password":"fixture","host":"::ffff:127.0.0.1","port":3306}`} {
		request("POST", probe, "owner", payload, common.CodeDataError)
	}
	for _, payload := range []string{`{"db_type":"sqlite"}`, `{"db_type":"mysql","sql":"DELETE FROM secret"}`, `{"port":3306,"port":3307}`} {
		request("POST", probe, "owner", payload, common.CodeArgumentError)
	}
	if snapshot("t_ai_api4conversations") != beforeSessions || snapshot("t_ai_documents") != beforeDocs {
		t.Fatal("denied/read-only requests changed rows")
	}
	// Tenant disable must also reject an otherwise active owner's API key.
	if err = db.Model(&entity.Tenant{}).Where("id=?", "owner").Update("status", inactive).Error; err != nil {
		t.Fatal(err)
	}
	request("DELETE", sessions, "owner-key", `{"delete_all":true}`, common.CodeUnauthorized)
	if err = db.Model(&entity.Tenant{}).Where("id=?", "owner").Update("status", active).Error; err != nil {
		t.Fatal(err)
	}
	// The second SQL DELETE fails after both statements execute: neither persists.
	var deletes atomic.Int32
	if err = db.Callback().Delete().After("gorm:delete").Register("after10bbe-g-fail", func(tx *gorm.DB) {
		if tx.Statement.Table == "t_ai_api4conversations" && deletes.Add(1) == 2 {
			tx.AddError(errors.New("private-sql-marker"))
		}
	}); err != nil {
		t.Fatal(err)
	}
	request("DELETE", sessions, "owner", `{"ids":["s-a","s-b"]}`, common.CodeServerError)
	if snapshot("t_ai_api4conversations") != beforeSessions {
		t.Fatal("bulk failure did not roll back")
	}
	if err = db.Callback().Delete().Remove("after10bbe-g-fail"); err != nil {
		t.Fatal(err)
	}
	running, cancelRunning := context.WithCancel(t.Context())
	defer cancelRunning()
	var cancelledDeletes atomic.Int32
	if err = db.Callback().Delete().After("gorm:delete").Register("after10bbe-g-cancel", func(tx *gorm.DB) {
		if tx.Statement.Table == "t_ai_api4conversations" && cancelledDeletes.Add(1) == 1 {
			cancelRunning()
		}
	}); err != nil {
		t.Fatal(err)
	}
	if _, code, err := service.NewAgentService().DeleteAgentSessions(running, "owner", "shared", service.DeleteAgentSessionsRequest{IDs: []string{"s-a", "s-b"}}); err == nil || code != common.CodeServerError || cancelledDeletes.Load() == 0 || snapshot("t_ai_api4conversations") != beforeSessions {
		t.Fatal("cancellation after DELETE did not roll back", code, err)
	}
	if err = db.Callback().Delete().Remove("after10bbe-g-cancel"); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if _, code, err := service.NewAgentService().DeleteAgentSessions(ctx, "owner", "shared", service.DeleteAgentSessionsRequest{DeleteAll: true}); err == nil || code != common.CodeServerError || snapshot("t_ai_api4conversations") != beforeSessions {
		t.Fatal("cancelled bulk delete side effect", code, err)
	}
	if _, err := dao.NewAPITokenDAO().GetUserByAPITokenContext(ctx, "owner-key"); err == nil {
		t.Fatal("token lookup ignored cancellation")
	}
	ids, err := dao.NewAPI4ConversationDAO().ListIDsByAgentID(t.Context(), "shared")
	if err != nil || strings.Join(ids, ",") != "s-a,s-b" {
		t.Fatal("DAO source scope", ids, err)
	}
	request("DELETE", sessions, "owner", `{"ids":["s-chat","s-other","missing"]}`, common.CodeDataError)
	result := request("DELETE", sessions, "owner-key", `{"ids":["s-a","s-a","s-chat","s-other","missing"],"delete_all":true}`, common.CodeSuccess)
	data := result["data"].(map[string]any)
	if data["success_count"] != json.Number("1") || len(data["errors"].([]any)) != 4 {
		t.Fatal("incomplete partial receipt", result)
	}
	var explicitScope int
	if err = read.QueryRow("SELECT count(*) FROM t_ai_api4conversations WHERE id='s-b'").Scan(&explicitScope); err != nil || explicitScope != 1 {
		t.Fatal("delete_all expanded explicit IDs")
	}
	request("DELETE", sessions, "owner", `{"delete_all":true}`, common.CodeSuccess)
	var remaining string
	if err = read.QueryRow("SELECT string_agg(id,',' ORDER BY id) FROM t_ai_api4conversations").Scan(&remaining); err != nil || remaining != "s-chat,s-disabled,s-other" {
		t.Fatal("cross-source/parent delete", remaining, err)
	}
	// Fail after a real document UPDATE and require independent rollback.
	if err = db.Callback().Update().After("gorm:update").Register("after10bbe-g-fail", func(tx *gorm.DB) {
		if tx.Statement.Table == "t_ai_documents" {
			tx.AddError(errors.New("private-sql-marker"))
		}
	}); err != nil {
		t.Fatal(err)
	}
	request("PUT", metadata, "owner", `{"metadata":[]}`, common.CodeServerError)
	if snapshot("t_ai_documents") != beforeDocs {
		t.Fatal("metadata update failure persisted")
	}
	if err = db.Callback().Update().Remove("after10bbe-g-fail"); err != nil {
		t.Fatal(err)
	}
	metadataContext, cancelMetadata := context.WithCancel(t.Context())
	defer cancelMetadata()
	var cancelledUpdates atomic.Int32
	if err = db.Callback().Update().After("gorm:update").Register("after10bbe-g-cancel", func(tx *gorm.DB) {
		if tx.Statement.Table == "t_ai_documents" {
			cancelledUpdates.Add(1)
			cancelMetadata()
		}
	}); err != nil {
		t.Fatal(err)
	}
	if _, code, err := service.NewDatasetsService().UpdateDocumentMetadataConfig(metadataContext, "owner", kb, "doc", service.DocumentMetadataConfigRequest{Metadata: json.RawMessage(`[]`)}); err == nil || code != common.CodeServerError || cancelledUpdates.Load() != 1 || snapshot("t_ai_documents") != beforeDocs {
		t.Fatal("cancellation after UPDATE did not roll back", code, err)
	}
	if err = db.Callback().Update().Remove("after10bbe-g-cancel"); err != nil {
		t.Fatal(err)
	}
	var docReads atomic.Int32
	if err = db.Callback().Query().After("gorm:query").Register("after10bbe-g-readback", func(tx *gorm.DB) {
		if tx.Statement.Table == "t_ai_documents" && docReads.Add(1) == 2 {
			tx.AddError(errors.New("private-sql-marker"))
		}
	}); err != nil {
		t.Fatal(err)
	}
	request("PUT", metadata, "owner", `{"metadata":[]}`, common.CodeServerError)
	if snapshot("t_ai_documents") != beforeDocs {
		t.Fatal("failed metadata readback committed")
	}
	if err = db.Callback().Query().Remove("after10bbe-g-readback"); err != nil {
		t.Fatal(err)
	}
	if _, code, err := service.NewDatasetsService().UpdateDocumentMetadataConfig(ctx, "owner", kb, "doc", service.DocumentMetadataConfigRequest{Metadata: json.RawMessage(`[]`)}); err == nil || code != common.CodeServerError || snapshot("t_ai_documents") != beforeDocs {
		t.Fatal("cancelled metadata side effect", code, err)
	}
	// An independent parser writer holds the document lock before metadata PUT.
	writer, err := read.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer writer.Rollback()
	if _, err = writer.Exec(`SELECT id FROM t_ai_documents WHERE id='doc' FOR UPDATE`); err != nil {
		t.Fatal(err)
	}
	if _, err = writer.Exec(`UPDATE t_ai_documents SET parser_config=parser_config || '{"concurrent_model_id":9007199254740995,"pages":[7]}'::jsonb WHERE id='doc'`); err != nil {
		t.Fatal(err)
	}
	started := make(chan struct{}, 1)
	if err = db.Callback().Query().Before("gorm:query").Register("after10bbe-g-lock", func(tx *gorm.DB) {
		if tx.Statement.Table == "t_ai_documents" {
			select {
			case started <- struct{}{}:
			default:
			}
		}
	}); err != nil {
		t.Fatal(err)
	}
	complete := make(chan error, 1)
	go func() {
		_, code, err := service.NewDatasetsService().UpdateDocumentMetadataConfig(t.Context(), "owner", kb, "doc", service.DocumentMetadataConfigRequest{Metadata: json.RawMessage(`{"type":"object","properties":{"n":{"type":"number","default":9007199254740997},"zero":{"default":0},"off":{"default":false}}}`)})
		if err == nil && code != common.CodeSuccess {
			err = fmt.Errorf("code %d", code)
		}
		complete <- err
	}()
	select {
	case <-started:
	case <-time.After(3 * time.Second):
		t.Fatal("metadata did not reach row lock")
	}
	select {
	case err := <-complete:
		t.Fatal("metadata ignored held document lock", err)
	case <-time.After(50 * time.Millisecond):
	}
	if err = writer.Commit(); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-complete:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("metadata lock not released")
	}
	if err = db.Callback().Query().Remove("after10bbe-g-lock"); err != nil {
		t.Fatal(err)
	}
	var config string
	if err = read.QueryRow(`SELECT parser_config::text FROM t_ai_documents WHERE id='doc'`).Scan(&config); err != nil {
		t.Fatal(err)
	}
	for _, value := range []string{"9007199254740993", "9007199254740995", "9007199254740997", `"pages": [7]`, `"zero": 0`, `"enabled": false`} {
		if !strings.Contains(config, value) {
			t.Fatal("lost parser field or number", config)
		}
	}
	// Active admin may manage the template, including complete [] replacement.
	result = request("PUT", metadata, "manager", `{"metadata":[{"key":"count","type":"number","enum":[0,9007199254740993],"restrict_values":false}]}`, common.CodeSuccess)
	if !strings.Contains(fmt.Sprint(result["data"]), "9007199254740993") {
		t.Fatal("response rounded number")
	}
	request("PUT", metadata, "owner-key", `{"metadata":[]}`, common.CodeSuccess)
	var empty bool
	if err = read.QueryRow(`SELECT parser_config->'metadata'='[]'::jsonb AND parser_config->>'model_id'='9007199254740993' AND parser_config->>'concurrent_model_id'='9007199254740995' FROM t_ai_documents WHERE id='doc'`).Scan(&empty); err != nil || !empty {
		t.Fatal("template replacement or preserved fields", err)
	}
	j.Event("acceptance-independent-SQL-readback", map[string]any{"jwt_api_auth": true, "owner_bulk_team_denied": true, "strict_omitted_empty_null": true, "source_parent_scope": true, "partial_receipt": true, "bulk_storage_failure_atomic": true, "document_manage_auth_before_read": true, "metadata_write_readback_failure_atomic": true, "document_lock_preserves_concurrent_parser": true, "json_numbers_false_zero_empty_array": true, "cancel_zero_side_effect": true, "probe_policy_read_only": true})
}
