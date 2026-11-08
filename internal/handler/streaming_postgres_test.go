package handler

import (
	"bufio"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
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
	"multirag/internal/engine"
	"multirag/internal/entity"
	"multirag/internal/service"
	"multirag/internal/testsupport"
)

// This fixture owns a database; it never falls back to application tables.
func streamingScratchDB(t *testing.T) (*gorm.DB, *sql.DB, *testsupport.CaseJournal) {
	t.Helper()
	path := os.Getenv("MULTIRAG_S_CASE_CONFIG")
	if path == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("private SSE/skill scratch config required")
		}
		t.Skip("requires owned PostgreSQL for SSE/skill consumers")
	}
	journal := testsupport.NewCase(t)
	var cfg struct {
		AdminDSN   string `json:"admin_dsn"`
		PGEndpoint string `json:"pg_endpoint"`
	}
	raw, err := os.ReadFile(path)
	if err != nil || json.Unmarshal(raw, &cfg) != nil {
		t.Fatal("private scratch config unavailable")
	}
	u, err := url.Parse(cfg.AdminDSN)
	if err != nil || u.Host != cfg.PGEndpoint || u.Hostname() != "127.0.0.1" || u.Path != "/postgres" {
		t.Fatal("explicit local admin endpoint required")
	}
	admin, err := sql.Open("pgx", cfg.AdminDSN)
	if err != nil {
		t.Fatal("open admin connection")
	}
	t.Cleanup(func() { admin.Close() })
	name := "multirag_after22_s_" + strings.ReplaceAll(uuid.NewString(), "-", "")[:16]
	journal.Event("reserve-database", map[string]any{"endpoint": u.Host, "database": name, "config_file": path})
	var present bool
	if err = admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&present); err != nil || present {
		t.Fatal("scratch absence precondition")
	}
	t.Cleanup(func() {
		_, dropErr := admin.Exec(`DROP DATABASE IF EXISTS "` + name + `" WITH (FORCE)`)
		readErr := admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&present)
		journal.Event("cleanup-database-readback", map[string]any{"database": name, "absent": !present, "drop_ok": dropErr == nil, "read_ok": readErr == nil})
		if dropErr != nil || readErr != nil || present {
			t.Error("scratch database cleanup failed")
		}
	})
	if _, err = admin.Exec(`CREATE DATABASE "` + name + `"`); err != nil {
		t.Fatal("create scratch database", err)
	}
	u.Path = "/" + name
	db, err := gorm.Open(postgres.Open(u.String()), &gorm.Config{Logger: logger.Discard})
	if err != nil {
		t.Fatal("connect scratch database")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	read, err := sql.Open("pgx", u.String())
	if err != nil {
		t.Fatal("independent SQL reader")
	}
	t.Cleanup(func() { read.Close() })
	previous := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = previous })
	return db, read, journal
}

func TestChatSessionSSEDeadlineScratchPostgres(t *testing.T) {
	db, read, journal := streamingScratchDB(t)
	if err := db.AutoMigrate(&entity.Chat{}, &entity.ChatSession{}, &entity.Tenant{}, &entity.User{}, &entity.UserTenant{}, &entity.TenantModelProvider{}, &entity.TenantModelInstance{}, &entity.TenantModel{}); err != nil {
		t.Fatal(err)
	}
	providerStopped := make(chan struct{})
	next := make(chan struct{})
	var calls atomic.Int32
	fixture := streamingHTTPServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var body struct{ Messages []struct{ Content string } }
		if r.URL.Path != "/chat/completions" || r.Header.Get("Authorization") != "Bearer fixture-key" || json.NewDecoder(r.Body).Decode(&body) != nil || len(body.Messages) == 0 {
			t.Error("incorrect provider binding")
			return
		}
		message := body.Messages[len(body.Messages)-1].Content
		w.Header().Set("Content-Type", "text/event-stream")
		select {
		case <-time.After(150 * time.Millisecond):
		case <-r.Context().Done():
			return
		}
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"first\"}}]}\n\n")
		w.(http.Flusher).Flush()
		if message == "cancel" {
			<-r.Context().Done()
			close(providerStopped)
			return
		}
		select {
		case <-next:
		case <-r.Context().Done():
			return
		}
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\" second\"}}]}\n\ndata: [DONE]\n\n")
	}), 0)
	catalog, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	catalog.FindProvider("Moonshot").URL["default"] = fixture.URL
	provider := service.NewModelProviderServiceWithCatalog(catalog, nil)
	active, actor := "1", "actor"
	journal.Event("reserve-fixture-rows", map[string]any{"tenant": actor, "dialog": "dialog", "sessions": []string{"complete", "cancel"}, "provider": "provider", "instance": "instance"})
	for _, row := range []any{
		&entity.User{ID: actor, Email: "sse@fixture.invalid", Status: &active},
		&entity.Tenant{ID: actor, Status: &active},
		&entity.UserTenant{ID: "membership", UserID: actor, TenantID: actor, Role: "owner", Status: &active},
		&entity.TenantModelProvider{ID: "provider", TenantID: actor, ProviderName: "Moonshot"},
		&entity.TenantModelInstance{ID: "instance", ProviderID: "provider", InstanceName: "default", APIKey: "fixture-key", Extra: `{}`},
		&entity.Chat{ID: "dialog", TenantID: actor, LLMID: "kimi-k2.5@default@Moonshot", LLMSetting: entity.JSONMap{}, PromptConfig: entity.JSONMap{}, KBIDs: entity.JSONSlice{}, Status: &active},
	} {
		if err := db.Create(row).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, id := range []string{"complete", "cancel"} {
		if err := db.Create(&entity.ChatSession{ID: id, DialogID: "dialog", UserID: &actor, Message: json.RawMessage(`{"messages":[]}`), Reference: json.RawMessage(`[]`)}).Error; err != nil {
			t.Fatal(err)
		}
	}
	if _, err := provider.GetChatModelWithContext(context.Background(), actor, "kimi-k2.5@default@Moonshot"); err != nil {
		t.Fatal("fixture model binding", err)
	}
	h := NewChatSessionHandler(service.NewChatSessionServiceWithModels(provider, service.NewChunkServiceWithModels(provider)), nil)
	handlerStopped := make(chan struct{})
	gin.SetMode(gin.TestMode)
	router := gin.New()
	router.Use(func(c *gin.Context) {
		if c.GetHeader("Authorization") == "Bearer identity" {
			c.Set("user", &entity.User{ID: actor})
		}
	})
	router.POST("/v1/conversation/completion", func(c *gin.Context) {
		if c.GetHeader("X-Fixture-Session") == "cancel" {
			defer close(handlerStopped)
		}
		h.Completion(c)
	})
	api := streamingHTTPServer(t, router, 40*time.Millisecond)
	post := func(ctx context.Context, id, message, auth string) *http.Response {
		t.Helper()
		body := fmt.Sprintf(`{"conversation_id":%q,"messages":[{"role":"user","content":%q,"id":"msg"}],"stream":true}`, id, message)
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, api.URL+"/v1/conversation/completion", strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", auth)
		req.Header.Set("X-Fixture-Session", id)
		resp, err := api.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		return resp
	}
	for _, tc := range []struct{ id, auth string }{{"complete", ""}, {"foreign", "Bearer identity"}} {
		resp := post(context.Background(), tc.id, "q", tc.auth)
		raw, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		var payload struct{ Code common.ErrorCode }
		if err != nil || json.Unmarshal(raw, &payload) != nil || payload.Code == common.CodeSuccess || !strings.HasPrefix(resp.Header.Get("Content-Type"), "application/json") || calls.Load() != 0 {
			t.Fatalf("authorization committed stream or reached provider: %s", raw)
		}
	}
	resp := post(context.Background(), "complete", "q", "Bearer identity")
	reader := bufio.NewReader(resp.Body)
	first, err := reader.ReadString('\n')
	if err != nil || !strings.Contains(first, `"answer":"first"`) || resp.Header.Get("Content-Type") != "text/event-stream" {
		t.Fatalf("first frame missing: %q %v", first, err)
	}
	close(next)
	raw, err := io.ReadAll(reader)
	resp.Body.Close()
	if err != nil || !strings.Contains(string(raw), `"answer":"first second"`) || !strings.Contains(string(raw), `"data":true`) {
		t.Fatalf("incomplete stream: %s %v", raw, err)
	}
	var stored string
	if err := read.QueryRow("SELECT message::text FROM t_ai_conversations WHERE id='complete'").Scan(&stored); err != nil || !strings.Contains(stored, "first second") {
		t.Fatal("independent committed SQL readback failed")
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	resp = post(ctx, "cancel", "cancel", "Bearer identity")
	first, err = bufio.NewReader(resp.Body).ReadString('\n')
	if err != nil || !strings.Contains(first, `"answer":"first"`) {
		t.Fatalf("cancel first frame: %q %v", first, err)
	}
	cancel()
	resp.Body.Close()
	select {
	case <-providerStopped:
	case <-time.After(time.Second):
		t.Fatal("provider HTTP body was not closed on client cancel")
	}
	select {
	case <-handlerStopped:
	case <-time.After(time.Second):
		t.Fatal("cancelled completion handler did not return")
	}
	if err := read.QueryRow("SELECT message::text FROM t_ai_conversations WHERE id='cancel'").Scan(&stored); err != nil || strings.Contains(stored, "first") {
		t.Fatal("cancel persisted partial success")
	}
	journal.Event("consumer-readback", map[string]any{"first_frame_flushed": true, "past_server_write_timeout": true, "sql_committed": true, "cancel_propagated": true, "cancel_sql_unchanged": true, "provider_boundary": "controlled HTTP; identity middleware fixture"})
}

type skillDeleteEngine struct {
	engine.SkillDocEngine
	drops atomic.Int32
}

func (e *skillDeleteEngine) DropTable(ctx context.Context, index string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	e.drops.Add(1)
	return nil
}

type skillDeleteStore struct {
	fail    atomic.Bool
	removed atomic.Int32
}

func (*skillDeleteStore) Put(string, string, []byte, ...string) error { return fmt.Errorf("not used") }
func (*skillDeleteStore) Get(string, string, ...string) ([]byte, error) {
	return nil, fmt.Errorf("not used")
}
func (s *skillDeleteStore) Remove(bucket, key string, args ...string) error {
	if s.fail.Load() {
		return fmt.Errorf("injected storage outage")
	}
	s.removed.Add(1)
	return nil
}

func TestSkillSpaceDeleteConsumerScratchPostgres(t *testing.T) {
	db, read, journal := streamingScratchDB(t)
	if err := db.AutoMigrate(&entity.File{}); err != nil {
		t.Fatal(err)
	}
	if err := dao.MigrateSkillCore(db); err != nil {
		t.Fatal(err)
	}
	doc, store := &skillDeleteEngine{}, &skillDeleteStore{}
	store.fail.Store(true)
	service.SetSkillCoreResources(doc, store)
	t.Cleanup(func() { service.SetSkillCoreResources(nil, nil) })
	location := "owned-object"
	journal.Event("reserve-fixture-rows", map[string]any{"space": "space", "tenant": "actor", "files": []string{"folder", "file", "foreign"}, "object": "controlled in-memory storage boundary; no external object created"})
	for _, row := range []any{
		&entity.SkillSpace{ID: "space", TenantID: "actor", Name: "owned", FolderID: "folder", Status: entity.SpaceStatusActive},
		&entity.SkillSearchConfig{ID: "config", TenantID: "actor", SpaceID: "space", Status: "1"},
		&entity.File{ID: "folder", TenantID: "actor", ParentID: "root", CreatedBy: "actor", Name: "owned", Type: "folder"},
		&entity.File{ID: "file", TenantID: "actor", ParentID: "folder", CreatedBy: "actor", Name: "SKILL.md", Type: "file", Location: &location},
		&entity.File{ID: "foreign", TenantID: "other", ParentID: "folder", CreatedBy: "other", Name: "foreign", Type: "file"},
	} {
		if err := db.Create(row).Error; err != nil {
			t.Fatal(err)
		}
	}
	h := NewSkillSearchHandler(doc)
	router := gin.New()
	router.Use(func(c *gin.Context) { c.Set("user", &entity.User{ID: c.GetHeader("X-Fixture-Actor")}) })
	group := router.Group("/api/v1/skill-core")
	group.Use(h.coreBoundary)
	group.DELETE("/spaces/:space_id", h.DeleteSpace)
	group.GET("/spaces/:space_id", h.GetSpace)
	api := streamingHTTPServer(t, router, time.Second)
	remove := func(actor string) (int, string) {
		t.Helper()
		req, _ := http.NewRequest(http.MethodDelete, api.URL+"/api/v1/skill-core/spaces/space", nil)
		req.Header.Set("X-Fixture-Actor", actor)
		resp, err := api.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		raw, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatal(err)
		}
		return resp.StatusCode, string(raw)
	}
	if status, body := remove("other"); status != http.StatusNotFound || doc.drops.Load() != 0 {
		t.Fatalf("foreign deletion reached cleanup: %d %s", status, body)
	}
	if status, body := remove("actor"); status != http.StatusAccepted || !strings.Contains(body, `"deleting":true`) {
		t.Fatalf("delete not accepted: %d %s", status, body)
	}
	await := func(check func() bool) {
		t.Helper()
		deadline := time.Now().Add(3 * time.Second)
		for time.Now().Before(deadline) {
			if check() {
				return
			}
			time.Sleep(10 * time.Millisecond)
		}
		t.Fatal("durable cleanup readback timed out")
	}
	await(func() bool {
		var status, state string
		err := read.QueryRow("SELECT status, core_state::text FROM t_ai_go_skill_spaces WHERE id='space'").Scan(&status, &state)
		return err == nil && status == entity.SpaceStatusDeleteFailed && strings.Contains(state, "delete_error")
	})
	var count int
	if err := read.QueryRow("SELECT count(*) FROM t_ai_files WHERE id IN ('folder','file')").Scan(&count); err != nil || count != 2 {
		t.Fatal("failed deletion discarded durable file addresses")
	}
	// The HTTP request has already ended. A new service instance resumes persisted
	// deletion state without its Authorization header or its cancelled context.
	store.fail.Store(false)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); service.NewSkillSpaceService().RecoverDeletes(ctx, doc) }()
	t.Cleanup(func() { cancel(); <-done })
	await(func() bool {
		var status string
		return read.QueryRow("SELECT status FROM t_ai_go_skill_spaces WHERE id='space'").Scan(&status) == nil && status == entity.SpaceStatusDeleted
	})
	for _, query := range []string{"SELECT count(*) FROM t_ai_files WHERE tenant_id='actor'", "SELECT count(*) FROM t_ai_go_skill_search_configs WHERE tenant_id='actor' AND status='1'"} {
		if err := read.QueryRow(query).Scan(&count); err != nil || count != 0 {
			t.Fatal("owned records survived successful cleanup")
		}
	}
	if err := read.QueryRow("SELECT count(*) FROM t_ai_go_skill_search_configs WHERE id='config' AND tenant_id='actor' AND status='0'").Scan(&count); err != nil || count != 1 {
		t.Fatal("config cleanup did not preserve its soft-delete contract")
	}
	if err := read.QueryRow("SELECT count(*) FROM t_ai_files WHERE id='foreign'").Scan(&count); err != nil || count != 1 || store.removed.Load() != 1 || doc.drops.Load() != 2 {
		t.Fatal("cleanup lost tenant isolation or retry semantics")
	}
	journal.Event("consumer-readback", map[string]any{"accepted": true, "failure_preserved_rows": true, "restart_recovery": true, "foreign_unchanged": true, "independent_sql": true, "storage_index_boundary": "controlled faults; actual PostgreSQL and production handler/service/DAO"})
}
