//go:build integration

package service

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/cloudwego/eino/components/model"
	"github.com/cloudwego/eino/schema"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/redis/go-redis/v9"
	"github.com/spf13/viper"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/testsupport"
)

// SQL diagnostics retain only the server's SQLSTATE and primary message. The
// query callback is deliberately never called, so SQL parameters, DSNs and
// configured credentials cannot be serialized into case evidence.
type agentSQLDiagnosticLogger struct {
	logger.Interface
	journal *testsupport.CaseJournal
}

func (l *agentSQLDiagnosticLogger) Trace(_ context.Context, _ time.Time, _ func() (string, int64), err error) {
	if err == nil || errors.Is(err, gorm.ErrRecordNotFound) {
		return
	}
	fields := map[string]any{"error_type": fmt.Sprintf("%T", err)}
	var pgError *pgconn.PgError
	if errors.As(err, &pgError) {
		fields["sqlstate"] = pgError.Code
		fields["message"] = pgError.Message
	}
	l.journal.Event("agent-sql-operation-error", fields)
}

// This fixture owns one fresh PostgreSQL database and explicitly reserved Redis
// keys. It neither migrates a business database nor flushes shared Redis state.
type agentExecutionFixture struct {
	db                                      *gorm.DB
	read                                    *sql.DB
	redis                                   *redis.Client
	redisRead                               *redis.Client
	journal                                 *testsupport.CaseJournal
	owner, team, invited, revoked, disabled string
	keys                                    []string
	modelCalls                              atomic.Int32
}

func newAgentExecutionFixture(t *testing.T) *agentExecutionFixture {
	t.Helper()
	if os.Getenv("REQUIRE_SERVICES") != "1" {
		t.Fatal("selected agent integration tests require REQUIRE_SERVICES=1")
	}
	j := testsupport.NewCase(t)
	base := filepath.Join("..", "..", "configs", "service_conf.yaml")
	overlay := os.Getenv("MULTIRAG_AGENT_EXECUTION_CONFIG")
	if overlay == "" {
		t.Fatal("MULTIRAG_AGENT_EXECUTION_CONFIG must name the read-only local overlay")
	}
	cfg := viper.New()
	cfg.SetConfigFile(base)
	if cfg.ReadInConfig() != nil {
		t.Fatal("tracked agent test config unavailable")
	}
	cfg.SetConfigFile(overlay)
	if cfg.MergeInConfig() != nil {
		t.Fatal("read-only local service overlay required")
	}
	pgHost := cfg.GetString("postgresql.host")
	pgPort := cfg.GetInt("postgresql.port")
	redisHost := cfg.GetString("redis.host")
	redisPort := cfg.GetInt("redis.port")
	if host, port, err := net.SplitHostPort(redisHost); err == nil {
		redisHost = host
		var parsed int
		if _, err := fmt.Sscan(port, &parsed); err != nil {
			t.Fatal("invalid Redis port")
		}
		redisPort = parsed
	}
	if redisPort == 0 {
		redisPort = 6379
	}
	if net.ParseIP(pgHost) == nil || !net.ParseIP(pgHost).IsLoopback() || net.ParseIP(redisHost) == nil || !net.ParseIP(redisHost).IsLoopback() || pgPort < 1 || pgPort > 65535 || redisPort < 1 || redisPort > 65535 {
		t.Fatal("loopback PostgreSQL and Redis are required")
	}
	dbCfg := server.DatabaseConfig{Driver: "postgres", Host: pgHost, Port: pgPort, Username: cfg.GetString("postgresql.user"), Password: cfg.GetString("postgresql.password"), Database: "postgres", Schema: "public"}
	if dbCfg.Username == "" {
		t.Fatal("PostgreSQL role required")
	}
	name := "multirag_after22_a_agent_" + agentTestID()
	tables := []string{"usr_ai.t_ai_users", "usr_ai.t_ai_tenants", "usr_ai.t_ai_user_tenants", "usr_ai.t_ai_user_canvases", "usr_ai.t_ai_user_canvas_version"}
	j.Event("reserve-agent-scratch-before-create", map[string]any{"database": name, "schema": "usr_ai", "tables": tables, "postgres_endpoint": net.JoinHostPort(pgHost, fmt.Sprint(pgPort)), "redis_endpoint": net.JoinHostPort(redisHost, fmt.Sprint(redisPort)), "redis_db": cfg.GetInt("redis.db"), "config_files": []string{base, overlay}})
	dsn, err := dao.PostgresDSN(dbCfg)
	if err != nil {
		t.Fatal("admin DSN configuration invalid")
	}
	admin, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("PostgreSQL admin connection unavailable")
	}
	t.Cleanup(func() { _ = admin.Close() })
	ready, cancel := context.WithTimeout(t.Context(), 3*time.Second)
	defer cancel()
	if admin.PingContext(ready) != nil {
		t.Fatal("required PostgreSQL unavailable")
	}
	var exists bool
	if err := admin.QueryRowContext(ready, "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&exists); err != nil || exists {
		t.Fatal("scratch database must be absent")
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_, dropErr := admin.ExecContext(ctx, `DROP DATABASE IF EXISTS "`+name+`" WITH (FORCE)`)
		var present bool
		readErr := admin.QueryRowContext(ctx, "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&present)
		j.Event("agent-scratch-cleanup-readback", map[string]any{"database": name, "schema": "usr_ai", "tables": tables, "absent": dropErr == nil && readErr == nil && !present})
		if dropErr != nil || readErr != nil || present {
			t.Error("independent agent scratch cleanup failed")
		}
	})
	if _, err := admin.ExecContext(ready, `CREATE DATABASE "`+name+`"`); err != nil {
		t.Fatal("create owned scratch database failed")
	}
	dbCfg.Database = name
	dbCfg.Schema = "usr_ai"
	dsn, err = dao.PostgresDSN(dbCfg)
	if err != nil {
		t.Fatal("scratch DSN invalid")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: &agentSQLDiagnosticLogger{Interface: logger.Default.LogMode(logger.Silent), journal: j}})
	if err != nil {
		t.Fatal("owned PostgreSQL connection unavailable")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = pool.Close() })
	read, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("independent PostgreSQL reader unavailable")
	}
	t.Cleanup(func() { _ = read.Close() })
	f := &agentExecutionFixture{db: db, read: read, journal: j, owner: agentTestID(), team: agentTestID(), invited: agentTestID(), revoked: agentTestID(), disabled: agentTestID()}
	f.exec(t, `CREATE SCHEMA usr_ai;
 CREATE TABLE usr_ai.t_ai_users(id varchar(32) PRIMARY KEY,nickname text,email text,status varchar(1),is_active boolean NOT NULL);
 CREATE TABLE usr_ai.t_ai_tenants(id varchar(32) PRIMARY KEY,status varchar(1));
 CREATE TABLE usr_ai.t_ai_user_tenants(id varchar(32) PRIMARY KEY,user_id varchar(32),tenant_id varchar(32),role text,status varchar(1));
 CREATE TABLE usr_ai.t_ai_user_canvases(id varchar(32) PRIMARY KEY,avatar text,user_id varchar(255) NOT NULL,tags varchar(512) NOT NULL DEFAULT '',title varchar(255),permission varchar(16) NOT NULL DEFAULT 'me',release boolean NOT NULL DEFAULT false,description text,canvas_type varchar(32),canvas_category varchar(32) NOT NULL DEFAULT 'agent_canvas',dsl jsonb,create_time bigint,create_date timestamptz,update_time bigint,update_date timestamptz);
 CREATE TABLE usr_ai.t_ai_user_canvas_version(id varchar(32) PRIMARY KEY,release boolean NOT NULL DEFAULT false,user_canvas_id varchar(255) NOT NULL,title varchar(255),description text,dsl jsonb,create_time bigint,create_date timestamptz,update_time bigint,update_date timestamptz);`)
	for _, id := range []string{f.owner, f.team, f.invited, f.revoked, f.disabled} {
		j.Event("reserve-agent-identity-rows", map[string]any{"database": name, "actor_id": id, "tenant_id": id, "tables": tables[:2]})
		if _, err := read.Exec("INSERT INTO t_ai_users(id,nickname,email,status,is_active) VALUES($1,'agent-test','agent-test@example.invalid','1',$2)", id, id != f.disabled); err != nil {
			t.Fatal(err)
		}
		if _, err := read.Exec("INSERT INTO t_ai_tenants(id,status) VALUES($1,'1')", id); err != nil {
			t.Fatal(err)
		}
	}
	for _, membership := range []struct{ actor, role, status string }{{f.team, "normal", "1"}, {f.invited, "invite", "1"}, {f.revoked, "normal", "0"}, {f.disabled, "normal", "1"}} {
		id := agentTestID()
		j.Event("reserve-agent-membership", map[string]any{"id": id, "user_id": membership.actor, "tenant_id": f.owner, "role": membership.role, "status": membership.status})
		if _, err := read.Exec("INSERT INTO t_ai_user_tenants(id,user_id,tenant_id,role,status) VALUES($1,$2,$3,$4,$5)", id, membership.actor, f.owner, membership.role, membership.status); err != nil {
			t.Fatal(err)
		}
	}
	options := &redis.Options{Addr: net.JoinHostPort(redisHost, fmt.Sprint(redisPort)), Password: cfg.GetString("redis.password"), DB: cfg.GetInt("redis.db")}
	f.redis = redis.NewClient(options)
	f.redisRead = redis.NewClient(options)
	t.Cleanup(func() { _ = f.redis.Close(); _ = f.redisRead.Close() })
	if f.redis.Ping(ready).Err() != nil {
		t.Fatal("required Redis unavailable")
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		var cleanupErr, errorRead error
		var remaining int64
		if len(f.keys) > 0 {
			cleanupErr = f.redis.Del(ctx, f.keys...).Err()
			remaining, errorRead = f.redisRead.Exists(ctx, f.keys...).Result()
		}
		j.Event("agent-redis-cleanup-readback", map[string]any{"keys": f.keys, "remaining": remaining, "confirmed": cleanupErr == nil && errorRead == nil && remaining == 0})
		if cleanupErr != nil || errorRead != nil || remaining != 0 {
			t.Error("independent exact Redis cleanup failed")
		}
	})
	previous := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = previous })
	return f
}

func agentTestID() string                  { return strings.ReplaceAll(uuid.NewString(), "-", "") }
func agentTestString(value string) *string { return &value }
func (f *agentExecutionFixture) exec(t *testing.T, query string, args ...any) {
	t.Helper()
	if err := f.db.Exec(query, args...).Error; err != nil {
		t.Fatal(err)
	}
}
func (f *agentExecutionFixture) service() *AgentService {
	s := NewAgentService()
	s.runClient = func() (*redis.Client, error) { return f.redis, nil }
	s.modelResolver = func(_ context.Context, tenant, id string) (model.ToolCallingChatModel, error) {
		f.modelCalls.Add(1)
		if tenant != f.owner || id != "9007199254740993" {
			return nil, fmt.Errorf("unexpected authorized model identity")
		}
		return &agentIntegrationModel{}, nil
	}
	return s
}
func (f *agentExecutionFixture) create(t *testing.T, s *AgentService, actor string, dsl json.RawMessage, permission string) *entity.UserCanvas {
	t.Helper()
	f.journal.Event("reserve-agent-create-in-owned-table", map[string]any{"actor_id": actor, "table": "usr_ai.t_ai_user_canvases"})
	row, code, err := s.CreateAgent(t.Context(), actor, &SaveAgentRequest{Title: agentTestString("created-" + agentTestID()[:8]), Permission: agentTestString(permission), DSL: dsl})
	if err != nil || code != common.CodeSuccess || row == nil {
		t.Fatalf("CreateAgent: %v %v", code, err)
	}
	return row
}
func (f *agentExecutionFixture) reserveRun(t *testing.T, s *AgentService) string {
	t.Helper()
	id := agentTestID()
	keys := []string{agentRecordKey(id), agentRecordKey(id) + ":lock", agentRecordKey(id) + ":cancel", "agent:cp:" + id}
	f.journal.Event("reserve-exact-agent-run-before-execution", map[string]any{"run_id": id, "keys": keys})
	if count, err := f.redisRead.Exists(t.Context(), keys...).Result(); err != nil || count != 0 {
		t.Fatal("owned run key precondition failed")
	}
	f.keys = append(f.keys, keys...)
	s.runID = func() string { return id }
	return id
}
func (f *agentExecutionFixture) run(t *testing.T, s *AgentService, actor, canvasID string, req *AgentRunRequest) []AgentRunEvent {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	events, code, err := s.RunAgent(ctx, actor, canvasID, req)
	if err != nil || code != common.CodeSuccess || events == nil {
		t.Fatalf("RunAgent: %v %v", code, err)
	}
	ensureAgentIntegrationRunCleanup(t, cancel, events, nil)
	var result []AgentRunEvent
	for {
		select {
		case event, ok := <-events:
			if !ok {
				return result
			}
			result = append(result, event)
		case <-ctx.Done():
			t.Fatal("agent execution did not close within bound")
			return nil
		}
	}
}
func (f *agentExecutionFixture) record(t *testing.T, id string) *agentExecutionRecord {
	t.Helper()
	raw, err := f.redisRead.Get(t.Context(), agentRecordKey(id)).Bytes()
	if err != nil {
		t.Fatal(err)
	}
	var record agentExecutionRecord
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&record); err != nil {
		t.Fatal(err)
	}
	return &record
}
func agentEvent(t *testing.T, events []AgentRunEvent, name string) AgentRunEvent {
	t.Helper()
	for _, event := range events {
		if event.Event == name {
			return event
		}
	}
	t.Fatalf("missing %s in %v", name, events)
	return AgentRunEvent{}
}
func assertAgentSuccess(t *testing.T, code common.ErrorCode, err error) {
	t.Helper()
	if code != common.CodeSuccess || err != nil {
		t.Fatalf("operation failed: %v %v", code, err)
	}
}
func assertAgentDenied(t *testing.T, code common.ErrorCode, err error) {
	t.Helper()
	if code == common.CodeSuccess || err == nil {
		t.Fatalf("operation unexpectedly succeeded: %v %v", code, err)
	}
}
func (f *agentExecutionFixture) versionCount(t *testing.T, id string) int {
	t.Helper()
	var count int
	if err := f.read.QueryRow("SELECT count(*) FROM t_ai_user_canvas_version WHERE user_canvas_id=$1", id).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}
func (f *agentExecutionFixture) canvasRelease(t *testing.T, id string) bool {
	t.Helper()
	var release bool
	if err := f.read.QueryRow("SELECT release FROM t_ai_user_canvases WHERE id=$1", id).Scan(&release); err != nil {
		t.Fatal(err)
	}
	return release
}
func agentMessageDSL(text string) json.RawMessage {
	raw, _ := json.Marshal(map[string]any{"version": 1, "components": map[string]any{"begin": map[string]any{"obj": map[string]any{"component_name": "Begin", "params": map[string]any{}}, "upstream": []string{}, "downstream": []string{"message"}}, "message": map[string]any{"obj": map[string]any{"component_name": "Message", "params": map[string]any{"text": text}}, "upstream": []string{"begin"}, "downstream": []string{}}}, "globals": map[string]any{"exact_id": json.Number("9007199254740993")}})
	return raw
}
func agentWaitingDSL() json.RawMessage {
	return json.RawMessage(`{"version":1,"components":{"begin":{"obj":{"component_name":"Begin","params":{}},"upstream":[],"downstream":["wait"]},"wait":{"obj":{"component_name":"UserFillUp","params":{"tips":"Please reply"}},"upstream":["begin"],"downstream":["message"]},"message":{"obj":{"component_name":"Message","params":{"text":"original {{sys.query}} / {{wait@user_input}}"}},"upstream":["wait"],"downstream":[]}},"globals":{"exact_id":9007199254740993}}`)
}

type agentIntegrationModel struct{}

func (*agentIntegrationModel) Generate(context.Context, []*schema.Message, ...model.Option) (*schema.Message, error) {
	return &schema.Message{Role: schema.Assistant, Content: "controlled model output"}, nil
}
func (m *agentIntegrationModel) Stream(ctx context.Context, messages []*schema.Message, opts ...model.Option) (*schema.StreamReader[*schema.Message], error) {
	message, err := m.Generate(ctx, messages, opts...)
	if err != nil {
		return nil, err
	}
	return schema.StreamReaderFromArray([]*schema.Message{message}), nil
}
func (m *agentIntegrationModel) WithTools([]*schema.ToolInfo) (model.ToolCallingChatModel, error) {
	return m, nil
}

func TestAgentExecutionIntegrationCRUDAndAuthorization(t *testing.T) {
	f := newAgentExecutionFixture(t)
	s := f.service()
	row := f.create(t, s, f.owner, agentMessageDSL("hello"), "team")
	for _, actor := range []string{f.owner, f.team} {
		if _, code, err := s.GetAgent(t.Context(), actor, row.ID); err != nil || code != common.CodeSuccess {
			t.Fatalf("read denied for accepted actor: %v %v", code, err)
		}
	}
	for _, actor := range []string{f.invited, f.revoked, f.disabled} {
		_, code, err := s.GetAgent(t.Context(), actor, row.ID)
		assertAgentDenied(t, code, err)
	}
	deniedTitle := "not-owned"
	for _, actor := range []string{f.team, f.invited, f.revoked, f.disabled} {
		code, err := s.UpdateAgent(t.Context(), actor, row.ID, &SaveAgentRequest{Title: &deniedTitle})
		assertAgentDenied(t, code, err)
		_, code, err = s.PublishAgent(t.Context(), actor, row.ID)
		assertAgentDenied(t, code, err)
		code, err = s.DeleteAgent(t.Context(), actor, row.ID)
		assertAgentDenied(t, code, err)
	}
	_, code, err := s.CreateAgent(t.Context(), f.disabled, &SaveAgentRequest{Title: agentTestString("disabled-create"), DSL: agentMessageDSL("unavailable")})
	assertAgentDenied(t, code, err)
	var disabledCanvases int
	if err := f.read.QueryRow("SELECT count(*) FROM t_ai_user_canvases WHERE user_id=$1", f.disabled).Scan(&disabledCanvases); err != nil || disabledCanvases != 0 {
		t.Fatal("disabled owner created an agent")
	}

	title := "updated"
	code, err = s.UpdateAgent(t.Context(), f.owner, row.ID, &SaveAgentRequest{Title: &title})
	assertAgentSuccess(t, code, err)
	var storedTitle string
	if err := f.read.QueryRow("SELECT title FROM t_ai_user_canvases WHERE id=$1", row.ID).Scan(&storedTitle); err != nil || storedTitle != title {
		t.Fatalf("update readback=%q %v", storedTitle, err)
	}
	version, code, err := s.PublishAgent(t.Context(), f.owner, row.ID)
	assertAgentSuccess(t, code, err)
	if version == nil || f.versionCount(t, row.ID) != 1 || !f.canvasRelease(t, row.ID) {
		t.Fatal("published version was not committed")
	}
	_, code, err = s.GetVersion(t.Context(), f.team, row.ID, version.ID)
	assertAgentSuccess(t, code, err)
	for _, actor := range []string{f.team, f.invited, f.revoked, f.disabled} {
		code, err = s.DeleteVersion(t.Context(), actor, row.ID, version.ID)
		assertAgentDenied(t, code, err)
	}

	// Both SQL and API readback must retain a numeric identifier beyond float64 precision.
	var exact, kind string
	if err := f.read.QueryRow("SELECT dsl #>> '{globals,exact_id}',jsonb_typeof(dsl #> '{globals,exact_id}') FROM t_ai_user_canvas_version WHERE id=$1", version.ID).Scan(&exact, &kind); err != nil || exact != "9007199254740993" || kind != "number" {
		t.Fatalf("published numeric JSON changed: %q %q %v", exact, kind, err)
	}
	published, code, err := s.GetVersion(t.Context(), f.owner, row.ID, version.ID)
	assertAgentSuccess(t, code, err)
	raw, err := json.Marshal(published.DSL)
	if err != nil {
		t.Fatal(err)
	}
	var decoded struct {
		Globals map[string]json.Number `json:"globals"`
	}
	if err := json.Unmarshal(raw, &decoded); err != nil || decoded.Globals["exact_id"].String() != "9007199254740993" {
		t.Fatalf("API version numeric precision lost: %s %v", raw, err)
	}
	other := f.create(t, s, f.owner, agentMessageDSL("other"), "me")
	code, err = s.DeleteVersion(t.Context(), f.owner, other.ID, version.ID)
	assertAgentDenied(t, code, err)
	if f.versionCount(t, row.ID) != 1 {
		t.Fatal("cross-canvas delete altered version")
	}
	// Renaming cannot silently duplicate an owner's other agent title.
	code, err = s.UpdateAgent(t.Context(), f.owner, row.ID, &SaveAgentRequest{Title: other.Title})
	assertAgentDenied(t, code, err)
	if err := f.read.QueryRow("SELECT title FROM t_ai_user_canvases WHERE id=$1", row.ID).Scan(&storedTitle); err != nil || storedTitle != "updated" {
		t.Fatal("failed duplicate rename changed title")
	}
	secondVersion, code, err := s.PublishAgent(t.Context(), f.owner, row.ID)
	assertAgentSuccess(t, code, err)
	code, err = s.UpdateAgent(t.Context(), f.owner, row.ID, &SaveAgentRequest{DSL: agentMessageDSL("unpublished draft")})
	assertAgentSuccess(t, code, err)
	if f.canvasRelease(t, row.ID) {
		t.Fatal("draft edit did not clear release")
	}
	code, err = s.DeleteVersion(t.Context(), f.owner, row.ID, secondVersion.ID)
	assertAgentSuccess(t, code, err)
	if f.versionCount(t, row.ID) != 1 || f.canvasRelease(t, row.ID) {
		t.Fatal("deleting a published snapshot re-released edited draft")
	}
	code, err = s.DeleteVersion(t.Context(), f.owner, row.ID, version.ID)
	assertAgentSuccess(t, code, err)
	if f.versionCount(t, row.ID) != 0 || f.canvasRelease(t, row.ID) {
		t.Fatal("last published version removal left release state")
	}
	// A disabled owner/tenant never remains readable through a TEAM membership.
	f.exec(t, "UPDATE t_ai_users SET is_active=false WHERE id=?", f.owner)
	_, code, err = s.GetAgent(t.Context(), f.team, row.ID)
	assertAgentDenied(t, code, err)
	f.exec(t, "UPDATE t_ai_users SET is_active=true WHERE id=?", f.owner)
	f.exec(t, "UPDATE t_ai_tenants SET status='0' WHERE id=?", f.owner)
	_, code, err = s.GetAgent(t.Context(), f.owner, row.ID)
	assertAgentDenied(t, code, err)
	f.exec(t, "UPDATE t_ai_tenants SET status='1' WHERE id=?", f.owner)
	code, err = s.DeleteAgent(t.Context(), f.owner, row.ID)
	assertAgentSuccess(t, code, err)
	var count int
	if err := f.read.QueryRow("SELECT count(*) FROM t_ai_user_canvases WHERE id=$1", row.ID).Scan(&count); err != nil || count != 0 {
		t.Fatal("delete did not commit")
	}
}

func TestAgentExecutionIntegrationTransactionRollback(t *testing.T) {
	f := newAgentExecutionFixture(t)
	s := f.service()
	row := f.create(t, s, f.owner, agentMessageDSL("before"), "me")
	f.journal.Event("reserve-agent-failure-trigger-before-create", map[string]any{"function": "usr_ai.agent_test_failure", "triggers": []string{"agent_block_publish", "agent_block_delete"}, "table": "usr_ai.t_ai_user_canvases", "canvas_id": row.ID})
	f.exec(t, `CREATE FUNCTION usr_ai.agent_test_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'controlled agent transaction failure'; END; $$`)
	f.exec(t, `CREATE TRIGGER agent_block_publish BEFORE UPDATE ON usr_ai.t_ai_user_canvases FOR EACH ROW WHEN (NEW.release = true) EXECUTE FUNCTION usr_ai.agent_test_failure()`)
	_, code, err := s.PublishAgent(t.Context(), f.owner, row.ID)
	assertAgentDenied(t, code, err)
	if f.versionCount(t, row.ID) != 0 || f.canvasRelease(t, row.ID) {
		t.Fatal("failed publish committed snapshot or release side effect")
	}
	f.exec(t, "DROP TRIGGER agent_block_publish ON usr_ai.t_ai_user_canvases")
	version, code, err := s.PublishAgent(t.Context(), f.owner, row.ID)
	assertAgentSuccess(t, code, err)
	f.exec(t, `CREATE TRIGGER agent_block_delete BEFORE DELETE ON usr_ai.t_ai_user_canvases FOR EACH ROW EXECUTE FUNCTION usr_ai.agent_test_failure()`)
	code, err = s.DeleteAgent(t.Context(), f.owner, row.ID)
	assertAgentDenied(t, code, err)
	if f.versionCount(t, row.ID) != 1 || !f.canvasRelease(t, row.ID) {
		t.Fatal("failed canvas delete did not roll back prior version deletion")
	}
	var retained string
	if err := f.read.QueryRow("SELECT id FROM t_ai_user_canvas_version WHERE id=$1", version.ID).Scan(&retained); err != nil || retained != version.ID {
		t.Fatal("version snapshot lost in rollback")
	}
	f.exec(t, "DROP TRIGGER agent_block_delete ON usr_ai.t_ai_user_canvases")
	code, err = s.DeleteAgent(t.Context(), f.owner, row.ID)
	assertAgentSuccess(t, code, err)
	if f.versionCount(t, row.ID) != 0 {
		t.Fatal("successful delete left versions")
	}
}

func TestAgentExecutionIntegrationRunAndImmutableResume(t *testing.T) {
	f := newAgentExecutionFixture(t)
	s := f.service()
	row := f.create(t, s, f.owner, agentMessageDSL("Echo {{sys.query}}"), "team")
	runID := f.reserveRun(t, s)
	events := f.run(t, s, f.owner, row.ID, &AgentRunRequest{Query: "hello"})
	if message := agentEvent(t, events, "message"); message.RunID != runID || message.Data["content"] != "Echo hello" {
		t.Fatalf("actual terminal message=%v", message)
	}
	agentEvent(t, events, "workflow_finished")
	if f.record(t, runID).Status != "succeeded" {
		t.Fatal("success was not durably recorded")
	}
	if remaining, err := f.redisRead.Exists(t.Context(), "agent:cp:"+runID, agentRecordKey(runID)+":lock").Result(); err != nil || remaining != 0 {
		t.Fatal("completed run retained lock/checkpoint")
	}
	waiting := f.create(t, s, f.owner, agentWaitingDSL(), "team")
	waitID := f.reserveRun(t, s)
	events = f.run(t, s, f.owner, waiting.ID, &AgentRunRequest{Query: "first"})
	pause := agentEvent(t, events, "waiting_for_user")
	interruptID, ok := pause.Data["interrupt_id"].(string)
	if !ok || interruptID == "" {
		t.Fatal("native interrupt ID missing")
	}
	before := f.record(t, waitID)
	if before.Status != "waiting" || before.ActorID != f.owner || before.CanvasID != waiting.ID {
		t.Fatal("paused ownership/status changed")
	}
	if exists, err := f.redisRead.Exists(t.Context(), "agent:cp:"+waitID).Result(); err != nil || exists != 1 {
		t.Fatal("native checkpoint not persisted")
	}
	// The accepted teammate can run this agent but cannot resume someone else's run.
	channel, code, err := s.RunAgent(t.Context(), f.team, waiting.ID, &AgentRunRequest{RunID: waitID, InterruptID: interruptID, Resume: "steal"})
	assertAgentDenied(t, code, err)
	if channel != nil {
		t.Fatal("foreign actor received event stream")
	}
	channel, code, err = s.RunAgent(t.Context(), f.owner, row.ID, &AgentRunRequest{RunID: waitID, InterruptID: interruptID, Resume: "wrong canvas"})
	assertAgentDenied(t, code, err)
	if channel != nil {
		t.Fatal("foreign canvas received event stream")
	}
	channel, code, err = s.RunAgent(t.Context(), f.owner, waiting.ID, &AgentRunRequest{RunID: waitID, InterruptID: "unknown-interrupt", Resume: "wrong interrupt"})
	assertAgentDenied(t, code, err)
	if channel != nil {
		t.Fatal("invalid interrupt accepted")
	}
	code, err = s.UpdateAgent(t.Context(), f.owner, waiting.ID, &SaveAgentRequest{DSL: agentMessageDSL("edited draft must not run")})
	assertAgentSuccess(t, code, err)
	events = f.run(t, s, f.owner, waiting.ID, &AgentRunRequest{RunID: waitID, InterruptID: interruptID, Resume: "reply"})
	if message := agentEvent(t, events, "message"); message.Data["content"] != "original first / reply" {
		t.Fatalf("resume used changed draft or lost prior state: %v", message)
	}
	after := f.record(t, waitID)
	if after.Status != "succeeded" || !bytes.Equal(before.DSL, after.DSL) {
		t.Fatal("resume replaced immutable execution DSL")
	}
	if f.modelCalls.Load() != 0 {
		t.Fatal("model-free graph invoked model resolver")
	}
}

func TestAgentExecutionIntegrationCancelAndSecurityOrdering(t *testing.T) {
	f := newAgentExecutionFixture(t)
	s := f.service()
	row := f.create(t, s, f.owner, agentWaitingDSL(), "team")
	runID := f.reserveRun(t, s)
	events := f.run(t, s, f.owner, row.ID, &AgentRunRequest{Query: "wait"})
	pause := agentEvent(t, events, "waiting_for_user")
	interruptID := pause.Data["interrupt_id"].(string)
	code, err := s.CancelAgent(t.Context(), f.team, row.ID, runID)
	assertAgentDenied(t, code, err)
	if count, err := f.redisRead.Exists(t.Context(), agentRecordKey(runID)+":cancel").Result(); err != nil || count != 0 {
		t.Fatal("foreign cancellation created side effects")
	}
	code, err = s.CancelAgent(t.Context(), f.owner, row.ID, runID)
	assertAgentSuccess(t, code, err)
	record := f.record(t, runID)
	if record.Status != "cancelled" || len(record.InterruptIDs) != 0 {
		t.Fatal("waiting cancellation was not final")
	}
	if count, err := f.redisRead.Exists(t.Context(), "agent:cp:"+runID).Result(); err != nil || count != 0 {
		t.Fatal("waiting cancellation retained checkpoint")
	}
	channel, code, err := s.RunAgent(t.Context(), f.owner, row.ID, &AgentRunRequest{RunID: runID, InterruptID: interruptID, Resume: "after cancel"})
	assertAgentDenied(t, code, err)
	if channel != nil {
		t.Fatal("cancelled run resumed")
	}
	reservedRun := f.reserveRun(t, s)
	channel, code, err = s.RunAgent(t.Context(), f.owner, row.ID, &AgentRunRequest{Inputs: map[string]any{"user_id": f.team}})
	if code != common.CodeArgumentError || err == nil || channel != nil {
		t.Fatalf("reserved identity input accepted: %v %v", code, err)
	}
	if count, err := f.redisRead.Exists(t.Context(), agentRecordKey(reservedRun), "agent:cp:"+reservedRun).Result(); err != nil || count != 0 {
		t.Fatal("reserved input created execution state")
	}
	var redisCalls, modelCalls atomic.Int32
	guarded := f.service()
	guarded.runClient = func() (*redis.Client, error) { redisCalls.Add(1); return nil, fmt.Errorf("must authorize SQL first") }
	guarded.modelResolver = func(context.Context, string, string) (model.ToolCallingChatModel, error) {
		modelCalls.Add(1)
		return nil, fmt.Errorf("unauthorized model call")
	}
	for _, actor := range []string{f.invited, f.revoked, f.disabled} {
		channel, code, err := guarded.RunAgent(t.Context(), actor, row.ID, &AgentRunRequest{Query: "denied"})
		assertAgentDenied(t, code, err)
		if channel != nil {
			t.Fatal("denied actor received events")
		}
	}
	if redisCalls.Load() != 0 || modelCalls.Load() != 0 {
		t.Fatal("unauthorized SQL access reached Redis or model")
	}
	// Accepted TEAM membership grants a new, actor-bound run and uses the owner's
	// tenant model identity. Only the model boundary is controlled in this test.
	modelDSL := json.RawMessage(`{"components":{"begin":{"obj":{"component_name":"Begin","params":{}},"upstream":[],"downstream":["llm"]},"llm":{"obj":{"component_name":"LLM","params":{"model_id":9007199254740993,"user_prompt":"{{sys.query}}"}},"upstream":["begin"],"downstream":["message"]},"message":{"obj":{"component_name":"Message","params":{"text":"{{llm@content}}"}},"upstream":["llm"],"downstream":[]}}}`)
	modelRow := f.create(t, s, f.owner, modelDSL, "team")
	modelRun := f.reserveRun(t, s)
	events = f.run(t, s, f.team, modelRow.ID, &AgentRunRequest{Query: "controlled request"})
	if message := agentEvent(t, events, "message"); message.Data["content"] != "controlled model output" {
		t.Fatalf("LLM chain did not return controlled boundary response: %v", message)
	}
	if f.modelCalls.Load() != 1 || f.record(t, modelRun).ActorID != f.team || f.record(t, modelRun).TenantID != f.owner {
		t.Fatal("model resolution or actor/owner isolation changed")
	}
}

// The hook pauses the actual finalization Eval before Redis executes it. The
// independent reader and cancel script keep working; no sleep approximates the
// otherwise narrow race between observing completion and committing run state.
type agentFinalizeBarrier struct {
	runID    string
	entered  chan struct{}
	release  chan struct{}
	gateOnce sync.Once
	openOnce sync.Once
}

func (b *agentFinalizeBarrier) DialHook(next redis.DialHook) redis.DialHook { return next }
func (b *agentFinalizeBarrier) ProcessPipelineHook(next redis.ProcessPipelineHook) redis.ProcessPipelineHook {
	return next
}
func (b *agentFinalizeBarrier) ProcessHook(next redis.ProcessHook) redis.ProcessHook {
	return func(ctx context.Context, cmd redis.Cmder) error {
		arguments := cmd.Args()
		scriptMatches := false
		if cmd.Name() == "eval" && len(arguments) > 1 {
			script, ok := arguments[1].(string)
			scriptMatches = ok && script == agentFinalizeScript
		}
		ownedRun := false
		for _, argument := range arguments {
			if key, ok := argument.(string); ok && key == agentRecordKey(b.runID) {
				ownedRun = true
				break
			}
		}
		gate := false
		if scriptMatches && ownedRun {
			b.gateOnce.Do(func() { gate = true; close(b.entered) })
		}
		if gate {
			select {
			case <-b.release:
			case <-ctx.Done():
				return ctx.Err()
			}
		}
		return next(ctx, cmd)
	}
}
func (b *agentFinalizeBarrier) open() { b.openOnce.Do(func() { close(b.release) }) }

func collectAgentIntegrationEvents(t *testing.T, ctx context.Context, events <-chan AgentRunEvent) []AgentRunEvent {
	t.Helper()
	var result []AgentRunEvent
	for {
		select {
		case event, ok := <-events:
			if !ok {
				return result
			}
			result = append(result, event)
		case <-ctx.Done():
			t.Fatal("barrier-controlled run did not finish")
			return nil
		}
	}
}

func assertAgentCancelledRecord(t *testing.T, f *agentExecutionFixture, runID string, events []AgentRunEvent) {
	t.Helper()
	agentEvent(t, events, "cancelled")
	for _, event := range events {
		if event.Event == "workflow_finished" {
			t.Fatal("cancelled run emitted successful completion")
		}
	}
	record := f.record(t, runID)
	if record.Status != "cancelled" || len(record.InterruptIDs) != 0 {
		t.Fatalf("cancellation lost during finalization: status=%s interrupts=%v", record.Status, record.InterruptIDs)
	}
	if remaining, err := f.redisRead.Exists(t.Context(), "agent:cp:"+runID, agentRecordKey(runID)+":lock").Result(); err != nil || remaining != 0 {
		t.Fatal("cancelled finalization retained checkpoint/lease")
	}
}

func TestAgentExecutionIntegrationCancelFinalizationRaces(t *testing.T) {
	for _, waiting := range []bool{false, true} {
		name := "running-record"
		if waiting {
			name = "waiting-record-with-active-lease"
		}
		t.Run(name, func(t *testing.T) {
			f := newAgentExecutionFixture(t)
			s := f.service()
			dsl := agentMessageDSL("completed graph")
			if waiting {
				dsl = agentWaitingDSL()
			}
			row := f.create(t, s, f.owner, dsl, "me")
			runID := f.reserveRun(t, s)
			f.journal.Event("reserve-agent-finalization-barrier", map[string]any{"run_id": runID, "script": "agentFinalizeScript", "waiting_transition": waiting})
			barrier := &agentFinalizeBarrier{runID: runID, entered: make(chan struct{}), release: make(chan struct{})}
			t.Cleanup(barrier.open)
			f.redis.AddHook(barrier)
			ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
			defer cancel()
			events, code, err := s.RunAgent(ctx, f.owner, row.ID, &AgentRunRequest{Query: "barrier"})
			assertAgentSuccess(t, code, err)
			if events == nil {
				t.Fatal("execution events missing")
			}
			ensureAgentIntegrationRunCleanup(t, cancel, events, barrier.open)
			select {
			case <-barrier.entered:
			case <-ctx.Done():
				t.Fatal("finalization Eval never reached registered barrier")
			}
			if count, err := f.redisRead.Exists(t.Context(), agentRecordKey(runID)+":lock").Result(); err != nil || count != 1 {
				t.Fatal("finalization barrier requires an active lease")
			}
			if record := f.record(t, runID); record.Status != "running" {
				t.Fatalf("expected prior running record, got %s", record.Status)
			}
			if waiting {
				// Reproduce a producer that persisted its waiting record while still
				// holding the execution lease. Cancel must flag the active run; the
				// pending finalizer must then atomically win over this stale waiting state.
				if count, err := f.redisRead.Exists(t.Context(), "agent:cp:"+runID).Result(); err != nil || count != 1 {
					t.Fatal("real UserFillUp checkpoint absent at barrier")
				}
				record := f.record(t, runID)
				record.Status = "waiting"
				record.InterruptIDs = []string{"barrier-persisted-interrupt"}
				f.journal.Event("inject-owned-waiting-record-with-held-lease", map[string]any{"run_id": runID, "key": agentRecordKey(runID)})
				if err := saveAgentExecution(t.Context(), f.redisRead, runID, record); err != nil {
					t.Fatal(err)
				}
			}
			code, err = s.CancelAgent(t.Context(), f.owner, row.ID, runID)
			assertAgentSuccess(t, code, err)
			expected := "running"
			if waiting {
				expected = "waiting"
			}
			if record := f.record(t, runID); record.Status != expected {
				t.Fatalf("active cancellation unexpectedly finalized before held lease: %s", record.Status)
			}
			if count, err := f.redisRead.Exists(t.Context(), agentRecordKey(runID)+":cancel").Result(); err != nil || count != 1 {
				t.Fatal("active cancellation signal was not durably committed")
			}
			barrier.open()
			received := collectAgentIntegrationEvents(t, ctx, events)
			assertAgentCancelledRecord(t, f, runID, received)
			f.journal.Event("agent-cancel-finalization-race-readback", map[string]any{"run_id": runID, "initial_status": expected, "final_status": "cancelled", "checkpoint_absent": true, "lease_absent": true})
		})
	}
}

type agentStalledModel struct {
	entered chan struct{}
	once    sync.Once
}

func (m *agentStalledModel) Generate(ctx context.Context, _ []*schema.Message, _ ...model.Option) (*schema.Message, error) {
	m.once.Do(func() { close(m.entered) })
	<-ctx.Done()
	return nil, ctx.Err()
}
func (m *agentStalledModel) Stream(ctx context.Context, messages []*schema.Message, opts ...model.Option) (*schema.StreamReader[*schema.Message], error) {
	_, err := m.Generate(ctx, messages, opts...)
	return nil, err
}
func (m *agentStalledModel) WithTools([]*schema.ToolInfo) (model.ToolCallingChatModel, error) {
	return m, nil
}

func TestAgentExecutionIntegrationCancelActiveModel(t *testing.T) {
	f := newAgentExecutionFixture(t)
	s := f.service()
	stalled := &agentStalledModel{entered: make(chan struct{})}
	s.modelResolver = func(_ context.Context, tenantID, modelID string) (model.ToolCallingChatModel, error) {
		if tenantID != f.owner || modelID != "9007199254740993" {
			return nil, fmt.Errorf("unexpected authorized stalled model identity")
		}
		return stalled, nil
	}
	dsl := json.RawMessage(`{"components":{"begin":{"obj":{"component_name":"Begin","params":{}},"upstream":[],"downstream":["llm"]},"llm":{"obj":{"component_name":"LLM","params":{"model_id":9007199254740993,"user_prompt":"{{sys.query}}"}},"upstream":["begin"],"downstream":["message"]},"message":{"obj":{"component_name":"Message","params":{"text":"{{llm@content}}"}},"upstream":["llm"],"downstream":[]}}}`)
	row := f.create(t, s, f.owner, dsl, "me")
	runID := f.reserveRun(t, s)
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	events, code, err := s.RunAgent(ctx, f.owner, row.ID, &AgentRunRequest{Query: "wait in controlled model"})
	assertAgentSuccess(t, code, err)
	ensureAgentIntegrationRunCleanup(t, cancel, events, nil)
	select {
	case <-stalled.entered:
	case <-ctx.Done():
		t.Fatal("controlled model was never invoked")
	}
	code, err = s.CancelAgent(t.Context(), f.owner, row.ID, runID)
	assertAgentSuccess(t, code, err)
	received := collectAgentIntegrationEvents(t, ctx, events)
	assertAgentCancelledRecord(t, f, runID, received)
}

// A failing assertion still joins the service goroutine before exact-key cleanup,
// preventing a late finalizer from recreating keys after the fixture deletes them.
func ensureAgentIntegrationRunCleanup(t *testing.T, cancel context.CancelFunc, events <-chan AgentRunEvent, release func()) {
	t.Helper()
	t.Cleanup(func() {
		if release != nil {
			release()
		}
		cancel()
		timer := time.NewTimer(7 * time.Second)
		defer timer.Stop()
		for {
			select {
			case _, ok := <-events:
				if !ok {
					return
				}
			case <-timer.C:
				t.Error("agent run survived cancellation and cleanup join")
				return
			}
		}
	})
}

func TestAgentExecutionIntegrationRejectsNativeFileForms(t *testing.T) {
	f := newAgentExecutionFixture(t)
	s := f.service()
	forms := map[string]string{
		"object": `{"upload":{"type":"file","value":null}}`,
		"list":   `[{"name":"upload","type":"FiLe[]","value":null}]`,
	}
	for name, inputs := range forms {
		t.Run(name, func(t *testing.T) {
			dsl := json.RawMessage(`{"components":{"wait":{"obj":{"component_name":"UserFillUp","params":{"inputs":` + inputs + `}},"upstream":[],"downstream":[]}}}`)
			row := f.create(t, s, f.owner, dsl, "me")
			runID := f.reserveRun(t, s)
			channel, code, err := s.RunAgent(t.Context(), f.owner, row.ID, &AgentRunRequest{Query: "file must not execute"})
			if code != common.CodeNotImplemented || err == nil || channel != nil {
				t.Fatalf("native file form was not rejected during compile: %v %v", code, err)
			}
			keys := agentExecutionKeys(runID)
			if count, err := f.redisRead.Exists(t.Context(), keys...).Result(); err != nil || count != 0 {
				t.Fatal("file compile failure mutated Redis execution state")
			}
			if f.modelCalls.Load() != 0 {
				t.Fatal("file compile failure invoked model resolver")
			}
			// A legacy queued file record also cannot bypass native validation on resume.
			// The marker is deliberately never a usable checkpoint: compile must reject
			// before it is read, leased, rewritten or passed into Eino.
			record := &agentExecutionRecord{ActorID: f.owner, CanvasID: row.ID, TenantID: f.owner, DSL: dsl, Input: map[string]any{"query": "legacy file"}, Status: "waiting", InterruptIDs: []string{"file-interrupt"}}
			f.journal.Event("inject-owned-legacy-file-record-before-resume", map[string]any{"run_id": runID, "keys": keys})
			if err := saveAgentExecution(t.Context(), f.redisRead, runID, record); err != nil {
				t.Fatal(err)
			}
			if err := f.redisRead.Set(t.Context(), "agent:cp:"+runID, "unread-file-checkpoint-marker", agentExecutionTTL).Err(); err != nil {
				t.Fatal(err)
			}
			before, err := f.redisRead.Get(t.Context(), agentRecordKey(runID)).Bytes()
			if err != nil {
				t.Fatal(err)
			}
			channel, code, err = s.RunAgent(t.Context(), f.owner, row.ID, &AgentRunRequest{RunID: runID, InterruptID: "file-interrupt", Resume: map[string]any{"upload": "opaque-file-id"}})
			if code != common.CodeNotImplemented || err == nil || channel != nil {
				t.Fatalf("queued file form resumed: %v %v", code, err)
			}
			after, err := f.redisRead.Get(t.Context(), agentRecordKey(runID)).Bytes()
			if err != nil || !bytes.Equal(before, after) {
				t.Fatal("rejected file resume rewrote metadata")
			}
			if marker, err := f.redisRead.Get(t.Context(), "agent:cp:"+runID).Result(); err != nil || marker != "unread-file-checkpoint-marker" {
				t.Fatal("rejected file resume touched checkpoint")
			}
			if count, err := f.redisRead.Exists(t.Context(), agentRecordKey(runID)+":lock", agentRecordKey(runID)+":cancel").Result(); err != nil || count != 0 {
				t.Fatal("rejected file resume created lease/cancel flag")
			}
			if f.modelCalls.Load() != 0 {
				t.Fatal("rejected file resume invoked resolver")
			}
		})
	}
}
