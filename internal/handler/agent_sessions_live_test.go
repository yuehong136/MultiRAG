package handler_test

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
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
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/testsupport"
	"multirag/internal/utility"
)

// TestAgentSessionsScratchPostgres exercises real JWT/API auth, HTTP and PostgreSQL.
func TestAgentSessionsScratchPostgres(t *testing.T) {
	path := os.Getenv("MULTIRAG_AGENT_SESSIONS_CONFIG")
	if path == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("private agent sessions config required")
		}
		t.Skip("requires owned scratch PostgreSQL")
	}
	journal := testsupport.NewCase(t)
	var cfg struct {
		AdminDSN   string `json:"admin_dsn"`
		PGEndpoint string `json:"pg_endpoint"`
		Listen     string `json:"listen"`
	}
	raw, err := os.ReadFile(path)
	if err != nil || json.Unmarshal(raw, &cfg) != nil {
		t.Fatal("private config unavailable")
	}
	u, err := url.Parse(cfg.AdminDSN)
	if err != nil || u.Host != "127.0.0.1:5432" || cfg.PGEndpoint != u.Host || !strings.HasPrefix(cfg.Listen, "127.0.0.1:") || strings.HasSuffix(cfg.Listen, ":0") {
		t.Fatal("explicit local endpoints required")
	}
	if err = server.FromConfigFile(path); err != nil {
		t.Fatal("private server config unavailable")
	}
	if err = server.InitVariables(nil); err != nil {
		t.Fatal("fixture signing key unavailable")
	}
	beforeAdmin := local.GetAdminStatus()
	local.SetAdminStatus(0, "agent session acceptance")
	t.Cleanup(func() { local.SetAdminStatus(beforeAdmin.Status, beforeAdmin.Reason) })
	admin, err := sql.Open("pgx", cfg.AdminDSN)
	if err != nil {
		t.Fatal("admin connection")
	}
	t.Cleanup(func() { admin.Close() })
	name := "multirag_after5e_a_" + strings.ReplaceAll(uuid.NewString(), "-", "")[:16]
	tables := []string{"t_ai_users", "t_ai_tenants", "t_ai_user_tenants", "t_ai_user_canvases", "t_ai_api_tokens", "t_ai_api4conversations"}
	journal.Event("reserve-database", map[string]any{"endpoint": u.Host, "database": name, "config_file": path, "tables": tables})
	var present bool
	if err = admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&present); err != nil || present {
		t.Fatal("database absence precondition", err)
	}
	t.Cleanup(func() {
		_, dropErr := admin.Exec(`DROP DATABASE IF EXISTS "` + name + `" WITH (FORCE)`)
		readErr := admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&present)
		journal.Event("cleanup-database", map[string]any{"endpoint": cfg.PGEndpoint, "database": name, "absent": !present, "drop_ok": dropErr == nil, "read_ok": readErr == nil})
		if dropErr != nil || readErr != nil || present {
			t.Error("database cleanup failed")
		}
	})
	if _, err = admin.Exec(`CREATE DATABASE "` + name + `"`); err != nil {
		t.Fatal("scratch create failed")
	}
	u.Path = "/" + name
	db, err := gorm.Open(postgres.Open(u.String()), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("scratch connect failed")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	reader, err := sql.Open("pgx", u.String())
	if err != nil {
		t.Fatal("independent reader failed")
	}
	t.Cleanup(func() { reader.Close() })
	oldDB := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = oldDB })
	journal.Event("reserve-schema", map[string]any{"database": name, "schema": "public", "tables": tables})
	if err = db.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.UserCanvas{}, &entity.APIToken{}, &entity.API4Conversation{}); err != nil {
		t.Fatal(err)
	}
	// Match the production SQLAlchemy DateTime(timezone=False) session columns.
	if err = db.Exec("ALTER TABLE t_ai_api4conversations ALTER COLUMN create_date TYPE timestamp without time zone USING create_date::timestamp without time zone, ALTER COLUMN update_date TYPE timestamp without time zone USING update_date::timestamp without time zone").Error; err != nil {
		t.Fatal(err)
	}
	principals := []string{"owner", "member", "admin", "invited", "revoked", "foreign", "disabled"}
	sessions := []string{"s-a", "s-b", "s-chat", "s-other", "s-private", "s-disabled"}
	journal.Event("reserve-rows", map[string]any{"database": name, "principals": principals, "canvases": []string{"shared", "private", "other", "disabled"}, "sessions": sessions, "api_tokens": []string{"owner-key", "foreign-key"}})
	active, inactive, access := "1", "0", "fixture-present-access"
	for _, id := range principals {
		if err = db.Create(&entity.User{ID: id, Nickname: id, Email: id + "@sessions.invalid", AccessToken: &access}).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err = db.Model(&entity.User{}).Where("id = ?", "disabled").Update("is_active", false).Error; err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"owner", "foreign", "disabled"} {
		if err = db.Create(&entity.Tenant{ID: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, row := range []struct {
		id, role string
		status   *string
	}{{"member", "normal", &active}, {"admin", "admin", &active}, {"invited", "invite", &active}, {"revoked", "normal", &inactive}} {
		if err = db.Create(&entity.UserTenant{ID: row.id, UserID: row.id, TenantID: "owner", Role: row.role, Status: row.status}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, canvas := range []entity.UserCanvas{{ID: "shared", UserID: "owner", Permission: "team"}, {ID: "private", UserID: "owner", Permission: "me"}, {ID: "other", UserID: "foreign", Permission: "me"}, {ID: "disabled", UserID: "disabled", Permission: "team"}} {
		if err = db.Create(&canvas).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, id := range []string{"owner", "foreign"} {
		if err = db.Create(&entity.APIToken{TenantID: id, Token: id + "-key"}).Error; err != nil {
			t.Fatal(err)
		}
	}
	stamp := time.Date(2026, 10, 29, 12, 0, 0, 0, time.Local)
	millis := stamp.UnixMilli()
	source, chat, exp := "agent", "chat", "explorer"
	for _, id := range sessions {
		agentID, userID, kind := "shared", "owner", &source
		switch id {
		case "s-a":
			userID = "foreign"
		case "s-chat":
			kind = &chat
		case "s-other":
			agentID = "other"
			userID = "foreign"
		case "s-private":
			agentID = "private"
		case "s-disabled":
			agentID = "disabled"
		}
		row := entity.API4Conversation{ID: id, DialogID: agentID, UserID: userID, Name: &id, ExpUserID: &exp, Source: kind, Message: json.RawMessage(`[{"role":"assistant","prompt":"private-start"},{"role":"user"},{"role":"assistant","content":"hello needle","prompt":"private-prompt","model_id":9007199254740993}]`), Reference: json.RawMessage(`[{"chunks":[null,"legacy-id",false,0,{"chunk_id":"c","doc_type":"image"}]}]`), Tokens: 9007199254740993, DSL: entity.JSONMap{}, BaseModel: entity.BaseModel{CreateDate: &stamp, UpdateDate: &stamp, CreateTime: &millis, UpdateTime: &millis}}
		if err = db.Create(&row).Error; err != nil {
			t.Fatal(err)
		}
		if _, err = reader.Exec("UPDATE t_ai_api4conversations SET dsl=$1::jsonb WHERE id=$2", `{"model_id":9007199254740993}`, id); err != nil {
			t.Fatal(err)
		}
	}
	snapshot := func() string {
		t.Helper()
		var value string
		if err := reader.QueryRow("SELECT COALESCE(json_agg(s ORDER BY id)::text,'[]') FROM t_ai_api4conversations s").Scan(&value); err != nil {
			t.Fatal(err)
		}
		return value
	}
	before := snapshot()
	gin.SetMode(gin.TestMode)
	routes := gin.New()
	routes.Use(handler.NewAuthHandler().AuthMiddleware())
	h := handler.NewAgentHandler(service.NewAgentService())
	routes.GET("/api/v1/agents/:agent_id/sessions", h.ListAgentSessions)
	routes.GET("/api/v1/agents/:agent_id/sessions/:session_id", h.GetAgentSession)
	routes.DELETE("/api/v1/agents/:agent_id/sessions/:session_id", h.DeleteAgentSessionItem)
	journal.Event("reserve-listener", map[string]any{"network": "tcp4", "address": cfg.Listen, "pid": os.Getpid()})
	listener, err := net.Listen("tcp4", cfg.Listen)
	if err != nil {
		t.Fatal(err)
	}
	httpServer := &http.Server{Handler: routes, ReadHeaderTimeout: time.Second}
	done := make(chan error, 1)
	go func() { done <- httpServer.Serve(listener) }()
	t.Cleanup(func() {
		httpServer.Close()
		if err := <-done; !errors.Is(err, http.ErrServerClosed) {
			t.Error(err)
		}
		probe, e := net.DialTimeout("tcp4", cfg.Listen, time.Second)
		absent := e != nil
		if probe != nil {
			probe.Close()
		}
		journal.Event("cleanup-listener", map[string]any{"address": cfg.Listen, "absent": absent})
		if !absent {
			t.Error("listener remains")
		}
	})
	client := &http.Client{Timeout: 5 * time.Second}
	request := func(method, path, principal string, want common.ErrorCode) map[string]any {
		t.Helper()
		req, err := http.NewRequest(method, "http://"+cfg.Listen+path, nil)
		if err != nil {
			t.Fatal(err)
		}
		if principal != "" {
			token := principal
			if !strings.HasSuffix(principal, "-key") {
				token, err = utility.SignUserJWT(principal+"@sessions.invalid", server.GetSecretKey())
				if err != nil {
					t.Fatal(err)
				}
			}
			req.Header.Set("Authorization", "Bearer "+token)
		}
		response, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		data, err := io.ReadAll(response.Body)
		if err != nil {
			t.Fatal(err)
		}
		decoder := json.NewDecoder(strings.NewReader(string(data)))
		decoder.UseNumber()
		var body map[string]any
		if err = decoder.Decode(&body); err != nil || body["code"] != json.Number(strings.TrimSpace(jsonNumberCode(want))) {
			t.Fatalf("business code status=%d body=%s want=%d", response.StatusCode, data, want)
		}
		return body
	}
	base := "/api/v1/agents/shared/sessions"
	request("GET", base, "", common.CodeUnauthorized)
	for _, actor := range []string{"owner", "member", "admin", "owner-key"} {
		body := request("GET", base+"?page_size=1", actor, common.CodeSuccess)
		data := body["data"].(map[string]any)
		if data["total"] != json.Number("2") || len(data["sessions"].([]any)) != 1 || data["sessions"].([]any)[0].(map[string]any)["id"] != "s-b" {
			t.Fatal(body)
		}
	}
	for _, actor := range []string{"foreign", "invited", "revoked", "foreign-key"} {
		request("GET", base+"?user_id=owner", actor, common.CodeOperatingError)
		request("DELETE", base+"/s-a", actor, common.CodeOperatingError)
	}
	request("GET", "/api/v1/agents/private/sessions", "member", common.CodeOperatingError)
	request("GET", "/api/v1/agents/disabled/sessions", "owner", common.CodeOperatingError)
	for _, query := range []string{"?page=0", "?page=bad", "?page=1&page=2", "?page=%zz", "?page=1;page_size=2", "?page_size=1001", "?orderby=source", "?desc=garbage", "?from_date=2026-02-30", "?from_date=2026-10-30&to_date=2026-10-29"} {
		request("GET", base+query, "owner", common.CodeArgumentError)
	}
	body := request("GET", base+"?page=2&page_size=1&keywords=NEEDLE&from_date=2026-10-29&to_date=2026-10-29&dsl=false", "owner", common.CodeSuccess)
	data := body["data"].(map[string]any)
	row := data["sessions"].([]any)[0].(map[string]any)
	if data["total"] != json.Number("2") || row["id"] != "s-a" {
		t.Fatal(body)
	}
	if _, ok := row["dsl"]; ok {
		t.Fatal("dsl=false ignored")
	}
	offset := time.FixedZone("fixture-offset", -4*60*60)
	fromOffset, toOffset := stamp.In(offset).Format(time.RFC3339), stamp.Add(time.Second).In(offset).Format(time.RFC3339)
	body = request("GET", base+"?from_date="+url.QueryEscape(fromOffset)+"&to_date="+url.QueryEscape(toOffset), "owner", common.CodeSuccess)
	if body["data"].(map[string]any)["total"] != json.Number("2") {
		t.Fatal("offset query mismatch against naive production timestamps", body)
	}
	body = request("GET", base+"?page_size=0", "owner", common.CodeSuccess)
	data = body["data"].(map[string]any)
	if data["total"] != json.Number("2") || len(data["sessions"].([]any)) != 0 {
		t.Fatal(body)
	}
	body = request("GET", base+"?user_id=foreign", "owner", common.CodeSuccess)
	data = body["data"].(map[string]any)
	if data["total"] != json.Number("1") || data["sessions"].([]any)[0].(map[string]any)["id"] != "s-a" {
		t.Fatal(body)
	}
	body = request("GET", base+"?exp_user_id=explorer", "member", common.CodeSuccess)
	data = body["data"].(map[string]any)
	if data["total"] != json.Number("2") || len(data["sessions"].([]any)[0].(map[string]any)) != 2 {
		t.Fatal(body)
	}
	body = request("GET", base+"/s-a", "owner", common.CodeSuccess)
	row = body["data"].(map[string]any)
	if row["agent_id"] != "shared" || row["tokens"] != json.Number("9007199254740993") || row["dsl"].(map[string]any)["model_id"] != json.Number("9007199254740993") {
		t.Fatal(body)
	}
	messages := row["messages"].([]any)
	for _, message := range messages {
		if _, ok := message.(map[string]any)["prompt"]; ok {
			t.Fatal("prompt leaked")
		}
	}
	refs := messages[2].(map[string]any)["reference"].([]any)
	if len(refs) != 3 || refs[1].(map[string]any)["id"] != json.Number("0") || refs[2].(map[string]any)["doc_type"] != "image" || refs[0].(map[string]any)["citation_id"] != "1" || refs[1].(map[string]any)["citation_id"] != "3" || refs[2].(map[string]any)["citation_id"] != "4" {
		t.Fatal(refs)
	}
	for _, id := range []string{"s-chat", "s-other", "missing"} {
		request("GET", base+"/"+id, "owner", common.CodeNotFound)
		body = request("DELETE", base+"/"+id, "owner", common.CodeSuccess)
		if body["data"] != false {
			t.Fatal(body)
		}
	}
	if snapshot() != before {
		t.Fatal("denial/read changed stored sessions")
	}
	// A real DELETE issued before an injected ORM failure must roll back.
	if err = db.Callback().Delete().After("gorm:delete").Before("gorm:commit_or_rollback_transaction").Register("sessions-fail", func(tx *gorm.DB) { tx.AddError(errors.New("private SQL credential marker")) }); err != nil {
		t.Fatal(err)
	}
	body = request("DELETE", base+"/s-a", "owner", common.CodeServerError)
	if snapshot() != before || strings.Contains(body["message"].(string), "private") {
		t.Fatal("rollback/error redaction", body)
	}
	if err = db.Callback().Delete().Remove("sessions-fail"); err != nil {
		t.Fatal(err)
	}
	cancelled, cancel := context.WithCancel(t.Context())
	cancel()
	if _, code, err := service.NewAgentService().DeleteAgentSessionItem(cancelled, "owner", "shared", "s-a"); err == nil || code != common.CodeServerError || snapshot() != before {
		t.Fatal("cancelled delete changed SQL", code, err)
	}
	body = request("DELETE", base+"/s-a", "owner-key", common.CodeSuccess)
	if body["data"] != true {
		t.Fatal(body)
	}
	var count int
	if err = reader.QueryRow("SELECT count(*) FROM t_ai_api4conversations WHERE id='s-a'").Scan(&count); err != nil || count != 0 {
		t.Fatal("independent delete readback", err, count)
	}
	if err = reader.QueryRow("SELECT count(*) FROM t_ai_api4conversations WHERE id IN ('s-chat','s-other','s-b','s-private','s-disabled')").Scan(&count); err != nil || count != 5 {
		t.Fatal("unrelated rows changed", err, count)
	}
	journal.Event("http-sql-readback", map[string]any{"jwt_api_tokens": true, "tenant_actor_team_access": true, "source_agent_binding": true, "stable_pagination_total": true, "filters_dates_dsl": true, "legacy_chunks_bigints": true, "prompt_redaction_nonmutation": true, "delete_rollback_cancellation_independent_readback": true})
}

func jsonNumberCode(code common.ErrorCode) string { raw, _ := json.Marshal(code); return string(raw) }
