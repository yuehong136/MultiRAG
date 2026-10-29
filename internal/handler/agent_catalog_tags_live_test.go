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
	"multirag/internal/service"
	"multirag/internal/testsupport"
)

// Actual handler/HTTP/PostgreSQL acceptance; principal injection is controlled.
// Production Router JWT/API authentication is a separate integration check.
func TestAgentCatalogTagsScratchPostgres(t *testing.T) {
	path := os.Getenv("MULTIRAG_AGENT_CATALOG_CONFIG")
	if path == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("private Agent catalogue config required")
		}
		t.Skip("requires scratch PostgreSQL")
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
	if err != nil || u.Host != "127.0.0.1:5432" || cfg.PGEndpoint != u.Host || !strings.HasPrefix(cfg.Listen, "127.0.0.1:") {
		t.Fatal("explicit TCP endpoints required")
	}
	admin, err := sql.Open("pgx", cfg.AdminDSN)
	if err != nil {
		t.Fatal("admin connection")
	}
	t.Cleanup(func() { admin.Close() })
	name := "multirag_after98_a_" + strings.ReplaceAll(uuid.NewString(), "-", "")[:16]
	journal.Event("reserve-database", map[string]any{"endpoint": u.Host, "database": name, "config_file": path, "tables": []string{"t_ai_user_canvases", "t_ai_canvas_templates", "t_ai_user_canvas_version"}})
	var present bool
	if err = admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&present); err != nil || present {
		t.Fatal("database absence precondition", err)
	}
	t.Cleanup(func() {
		_, dropErr := admin.Exec(`DROP DATABASE IF EXISTS "` + name + `" WITH (FORCE)`)
		readErr := admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&present)
		journal.Event("cleanup-database", map[string]any{"endpoint": cfg.PGEndpoint, "database": name, "absent": !present, "drop_ok": dropErr == nil, "read_ok": readErr == nil})
		if dropErr != nil || readErr != nil || present {
			t.Error("database cleanup", dropErr, readErr)
		}
	})
	if _, err = admin.Exec(`CREATE DATABASE "` + name + `"`); err != nil {
		t.Fatal("create database", err)
	}
	u.Path = "/" + name
	db, err := gorm.Open(postgres.Open(u.String()), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("scratch connect")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	reader, err := sql.Open("pgx", u.String())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { reader.Close() })
	old := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = old })
	if err = db.AutoMigrate(&entity.UserCanvas{}, &entity.CanvasTemplate{}, &entity.UserCanvasVersion{}); err != nil {
		t.Fatal(err)
	}
	ids := []string{"owned", "team", "other", "moved"}
	journal.Event("reserve-rows", map[string]any{"database": name, "canvases": ids, "templates": []string{"template-a", "template-b", "template-old"}, "versions": []string{"version-owned"}, "principals": []string{"owner", "reader", "foreign"}})
	stamp := int64(1)
	for _, id := range ids {
		owner := "owner"
		permission := "me"
		if id == "team" {
			owner = "foreign"
			permission = "team"
		}
		if err = db.Create(&entity.UserCanvas{ID: id, UserID: owner, Permission: permission, Tags: "protected", DSL: entity.JSONMap{"id": id}, Release: true, BaseModel: entity.BaseModel{UpdateTime: &stamp}}).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err = db.Create(&entity.UserCanvasVersion{ID: "version-owned", UserCanvasID: "owned", DSL: entity.JSONMap{"saved": true}}).Error; err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"template-b", "template-a", "template-old"} {
		created := int64(100)
		if id == "template-old" {
			created = 1
		}
		if err = db.Create(&entity.CanvasTemplate{ID: id, Title: entity.JSONMap{"zh": "标题", "en": "Title"}, Description: entity.JSONMap{"en": "Description"}, DSL: entity.JSONMap{"nodes": []any{"starter"}}, BaseModel: entity.BaseModel{CreateTime: &created}}).Error; err != nil {
			t.Fatal(err)
		}
	}
	snapshot := func(table, id string) string {
		t.Helper()
		var result string
		if err := reader.QueryRow("SELECT row_to_json(r)::text FROM "+table+" r WHERE id=$1", id).Scan(&result); err != nil {
			t.Fatal(err)
		}
		return result
	}
	unchangedFields := func(raw string) map[string]any {
		t.Helper()
		var fields map[string]any
		if err := json.Unmarshal([]byte(raw), &fields); err != nil {
			t.Fatal(err)
		}
		for _, key := range []string{"tags", "update_time", "update_date"} {
			delete(fields, key)
		}
		return fields
	}
	versionBefore := snapshot("t_ai_user_canvas_version", "version-owned")
	otherBefore := snapshot("t_ai_user_canvases", "other")
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(func(c *gin.Context) {
		if id := strings.TrimPrefix(c.GetHeader("Authorization"), "fixture:"); id != "" {
			c.Set("user", &entity.User{ID: id})
		}
	})
	h := handler.NewAgentHandler(service.NewAgentService())
	r.GET("/api/v1/agents/templates", h.ListTemplates)
	r.GET("/api/v1/agents/:agent_id/versions", h.ListAgentVersions)
	r.PUT("/api/v1/agents/:agent_id/tags", h.UpdateAgentTags)
	journal.Event("reserve-listener", map[string]any{"network": "tcp4", "address": cfg.Listen, "pid": os.Getpid()})
	listener, err := net.Listen("tcp4", cfg.Listen)
	if err != nil {
		t.Fatal(err)
	}
	server := &http.Server{Handler: r, ReadHeaderTimeout: time.Second}
	done := make(chan error, 1)
	go func() { done <- server.Serve(listener) }()
	t.Cleanup(func() {
		server.Close()
		serveErr := <-done
		if !errors.Is(serveErr, http.ErrServerClosed) {
			t.Error(serveErr)
		}
		probe, e := net.DialTimeout("tcp4", cfg.Listen, time.Second)
		absent := e != nil
		if probe != nil {
			probe.Close()
		}
		journal.Event("cleanup-listener", map[string]any{"address": cfg.Listen, "absent": absent})
		if !absent {
			t.Error("listener still present")
		}
	})
	client := &http.Client{Timeout: 5 * time.Second}
	request := func(method, path, body, caller string) map[string]any {
		t.Helper()
		req, e := http.NewRequest(method, "http://"+cfg.Listen+path, strings.NewReader(body))
		if e != nil {
			t.Fatal(e)
		}
		if caller != "" {
			req.Header.Set("Authorization", "fixture:"+caller)
		}
		req.Header.Set("Content-Type", "application/json")
		resp, e := client.Do(req)
		if e != nil {
			t.Fatal(e)
		}
		defer resp.Body.Close()
		raw, e := io.ReadAll(resp.Body)
		if e != nil {
			t.Fatal(e)
		}
		var result map[string]any
		if resp.StatusCode != 200 || json.Unmarshal(raw, &result) != nil {
			t.Fatalf("HTTP status/body %d %s", resp.StatusCode, raw)
		}
		return result
	}
	base := "/api/v1/agents"
	assertCode := func(body map[string]any, want common.ErrorCode) {
		t.Helper()
		if body["code"] != float64(want) {
			t.Fatalf("business response %+v want %d", body, want)
		}
	}
	assertCode(request("GET", base+"/templates", "", ""), common.CodeUnauthorized)
	result := request("GET", base+"/templates", "", "owner")
	assertCode(result, common.CodeSuccess)
	rows := result["data"].([]any)
	if len(rows) != 3 {
		t.Fatal(result)
	}
	for i, id := range []string{"template-a", "template-b", "template-old"} {
		row := rows[i].(map[string]any)
		if row["id"] != id || row["title"].(map[string]any)["zh"] != "标题" || row["dsl"] == nil {
			t.Fatal("locale/DSL/order", row)
		}
	}
	before := snapshot("t_ai_user_canvases", "owned")
	for _, payload := range []string{`{}`, `{"tags":null}`, `{"tags":1}`, `{"tags":{}}`, `{"tags":[1]}`, `{"tags":[null]}`, `{"tags":[],"owner":"foreign"}`, `{"tags":[],"tags":["x"]}`, `{"Tags":["x"]}`, `{"tags":[],"Tags":["x"]}`, `{"tags":[]} {}`} {
		assertCode(request("PUT", base+"/owned/tags", payload, "owner"), common.CodeArgumentError)
		if snapshot("t_ai_user_canvases", "owned") != before {
			t.Fatal("invalid payload wrote")
		}
	}
	assertCode(request("PUT", base+"/owned/tags", `{"tags":[]}`, ""), common.CodeUnauthorized)
	teamBefore := snapshot("t_ai_user_canvases", "team")
	for _, tc := range []struct{ id, caller string }{{"owned", "foreign"}, {"team", "reader"}, {"missing", "owner"}} {
		denied := request("PUT", base+"/"+tc.id+"/tags", `{"tags":["denied"]}`, tc.caller)
		assertCode(denied, common.CodeOperatingError)
		if denied["data"] != false {
			t.Fatal("denial boolean response", denied)
		}
	}
	if snapshot("t_ai_user_canvases", "owned") != before || snapshot("t_ai_user_canvases", "team") != teamBefore {
		t.Fatal("denied write")
	}
	result = request("PUT", base+"/owned/tags", `{"tags":[" ML ","ml","Data,Science","中文"]}`, "owner")
	assertCode(result, common.CodeSuccess)
	if result["data"] != true {
		t.Fatal(result)
	}
	var tags string
	var updated int64
	if err = reader.QueryRow("SELECT tags,update_time FROM t_ai_user_canvases WHERE id='owned'").Scan(&tags, &updated); err != nil || tags != "ML,Data Science,中文" || updated <= stamp {
		t.Fatal("independent success/time readback", tags, updated, err)
	}
	beforeFields, _ := json.Marshal(unchangedFields(before))
	afterFields, _ := json.Marshal(unchangedFields(snapshot("t_ai_user_canvases", "owned")))
	if string(beforeFields) != string(afterFields) {
		t.Fatal("tags update changed other columns")
	}
	for _, payload := range []string{`{"tags":[]}`, `{"tags":[]}`} {
		assertCode(request("PUT", base+"/owned/tags", payload, "owner"), common.CodeSuccess)
		if err = reader.QueryRow("SELECT tags FROM t_ai_user_canvases WHERE id='owned'").Scan(&tags); err != nil || tags != "" {
			t.Fatal("explicit empty did not clear", tags, err)
		}
	}
	assertCode(request("PUT", base+"/owned/tags", `{"tags":" a, b, A,, "}`, "owner"), common.CodeSuccess)
	if err = reader.QueryRow("SELECT tags FROM t_ai_user_canvases WHERE id='owned'").Scan(&tags); err != nil || tags != "a,b" {
		t.Fatal(tags, err)
	}
	// Persisted owner transfer invalidates an earlier owner read; team readability never grants mutation.
	if _, err = reader.Exec("UPDATE t_ai_user_canvases SET user_id='foreign' WHERE id='moved'"); err != nil {
		t.Fatal(err)
	}
	moved := snapshot("t_ai_user_canvases", "moved")
	assertCode(request("PUT", base+"/moved/tags", `{"tags":["stale-owner"]}`, "owner"), common.CodeOperatingError)
	if snapshot("t_ai_user_canvases", "moved") != moved {
		t.Fatal("stale owner wrote")
	}
	before = snapshot("t_ai_user_canvases", "owned")
	if err = db.Callback().Update().Before("gorm:update").Register("after98_fail", func(tx *gorm.DB) { tx.AddError(errors.New("private SQL credential marker")) }); err != nil {
		t.Fatal(err)
	}
	result = request("PUT", base+"/owned/tags", `{"tags":["rollback"]}`, "owner")
	assertCode(result, common.CodeServerError)
	if strings.Contains(result["message"].(string), "private") || snapshot("t_ai_user_canvases", "owned") != before {
		t.Fatal("failure leak/write", result)
	}
	if err = db.Callback().Update().Remove("after98_fail"); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	_, code, err := service.NewAgentService().UpdateAgentTags(ctx, "owner", "owned", []string{"cancelled"})
	if err == nil || code != common.CodeServerError || snapshot("t_ai_user_canvases", "owned") != before {
		t.Fatal("cancelled request wrote", code, err)
	}
	if snapshot("t_ai_user_canvas_version", "version-owned") != versionBefore || snapshot("t_ai_user_canvases", "other") != otherBefore {
		t.Fatal("unrelated/version changed")
	}
	if err = db.Where("1=1").Delete(&entity.CanvasTemplate{}).Error; err != nil {
		t.Fatal(err)
	}
	result = request("GET", base+"/templates", "", "owner")
	assertCode(result, common.CodeSuccess)
	if len(result["data"].([]any)) != 0 {
		t.Fatal("empty catalogue shape", result)
	}
	journal.Event("http-sql-readback", map[string]any{"templates_multilingual_dsl_stable": true, "strict_tags": true, "owner_only": true, "timestamp": updated, "explicit_empty": true, "rollback": true, "cancellation": true, "versions_other_canvas_unchanged": true, "principal": "controlled middleware; production Router/JWT separately"})
}
