//go:build integration

package handler_test

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
	"github.com/spf13/viper"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	redisengine "multirag/internal/engine/redis"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/service"
	"multirag/internal/testsupport"
)

// The hook reserves every exact key before the actual public service writes it.
// It never deletes shared Redis keys and does not replace business commands.
type agentWireRunJournal interface {
	Event(string, map[string]any)
}

type agentWireRedisReader interface {
	Exists(context.Context, ...string) *redis.IntCmd
}

type agentWireRedisJournal struct {
	mu   sync.Mutex
	j    agentWireRunJournal
	read agentWireRedisReader
	keys map[string]bool
}

func (h *agentWireRedisJournal) DialHook(next redis.DialHook) redis.DialHook { return next }
func (h *agentWireRedisJournal) ProcessPipelineHook(next redis.ProcessPipelineHook) redis.ProcessPipelineHook {
	return next
}
func (h *agentWireRedisJournal) ProcessHook(next redis.ProcessHook) redis.ProcessHook {
	return func(ctx context.Context, cmd redis.Cmder) error {
		if key, claims := agentWireLockClaim(cmd); claims {
			if strings.HasPrefix(key, "agent:execution:") && strings.HasSuffix(key, ":lock") {
				id := strings.TrimSuffix(strings.TrimPrefix(key, "agent:execution:"), ":lock")
				if !regexp.MustCompile(`^[0-9a-f]{32}$`).MatchString(id) {
					return fmt.Errorf("unexpected agent key")
				}
				h.mu.Lock()
				if !h.keys[key] {
					keys := agentWireRunKeys(id)
					h.j.Event("reserve-public-agent-run-before-redis-write", map[string]any{"run_id": id, "keys": keys})
					count, err := h.read.Exists(ctx, keys...).Result()
					if err != nil || count != 0 {
						h.mu.Unlock()
						return fmt.Errorf("owned public run key precondition failed")
					}
					for _, k := range keys {
						h.keys[k] = true
					}
				}
				h.mu.Unlock()
			}
		}
		return next(ctx, cmd)
	}
}

// This drives real HTTP/SSE, SQL and Redis. Principal middleware is a fixture;
// Router.Setup JWT/API-token authentication is a separate integration concern.
func TestAgentExecutionHTTPPostgresRedis(t *testing.T) {
	if os.Getenv("REQUIRE_SERVICES") != "1" || os.Getenv("MULTIRAG_AGENT_EXECUTION_CONFIG") == "" {
		t.Fatal("registered scratch config and REQUIRE_SERVICES=1 required")
	}
	j := testsupport.NewCase(t)
	v := viper.New()
	v.SetConfigFile(filepath.Join("..", "..", "configs", "service_conf.yaml"))
	if v.ReadInConfig() != nil {
		t.Fatal("base config required")
	}
	v.SetConfigFile(os.Getenv("MULTIRAG_AGENT_EXECUTION_CONFIG"))
	if v.MergeInConfig() != nil {
		t.Fatal("private overlay required")
	}
	pg := server.DatabaseConfig{Driver: "postgres", Host: v.GetString("postgresql.host"), Port: v.GetInt("postgresql.port"), Username: v.GetString("postgresql.user"), Password: v.GetString("postgresql.password"), Database: "postgres", Schema: "public"}
	rc := server.RedisConfig{Host: v.GetString("redis.host"), Port: v.GetInt("redis.port"), Password: v.GetString("redis.password"), DB: v.GetInt("redis.db")}
	if host, port, err := net.SplitHostPort(rc.Host); err == nil {
		rc.Host = host
		if _, err := fmt.Sscan(port, &rc.Port); err != nil {
			t.Fatal("invalid Redis endpoint")
		}
	}
	if rc.Port == 0 {
		rc.Port = 6379
	}
	if ip := net.ParseIP(pg.Host); ip == nil || !ip.IsLoopback() {
		t.Fatal("loopback PostgreSQL required")
	}
	if ip := net.ParseIP(rc.Host); ip == nil || !ip.IsLoopback() {
		t.Fatal("loopback Redis required")
	}
	name := "multirag_after22_a_wire_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	j.Event("reserve-public-agent-database-before-create", map[string]any{"database": name, "schema": "public", "tables": []string{"t_ai_users", "t_ai_tenants", "t_ai_user_tenants", "t_ai_user_canvases", "t_ai_user_canvas_version"}, "sql_endpoint": net.JoinHostPort(pg.Host, fmt.Sprint(pg.Port)), "redis_endpoint": net.JoinHostPort(rc.Host, fmt.Sprint(rc.Port)), "redis_db": rc.DB})
	dsn, err := dao.PostgresDSN(pg)
	if err != nil {
		t.Fatal("admin config invalid")
	}
	admin, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("admin connection failed")
	}
	t.Cleanup(func() { admin.Close() })
	ready, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	if admin.PingContext(ready) != nil {
		t.Fatal("required PostgreSQL unavailable")
	}
	var exists bool
	if err := admin.QueryRowContext(ready, "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&exists); err != nil || exists {
		t.Fatal("scratch database precondition")
	}
	t.Cleanup(func() {
		ctx, done := context.WithTimeout(context.Background(), 10*time.Second)
		defer done()
		_, drop := admin.ExecContext(ctx, `DROP DATABASE IF EXISTS "`+name+`" WITH (FORCE)`)
		read := admin.QueryRowContext(ctx, "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&exists)
		j.Event("public-agent-database-cleanup-readback", map[string]any{"database": name, "absent": drop == nil && read == nil && !exists})
		if drop != nil || read != nil || exists {
			t.Error("scratch database survived")
		}
	})
	if _, err := admin.ExecContext(ready, `CREATE DATABASE "`+name+`"`); err != nil {
		t.Fatal("create scratch database failed")
	}
	pg.Database = name
	dsn, err = dao.PostgresDSN(pg)
	if err != nil {
		t.Fatal("scratch config invalid")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("scratch connect failed")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	read, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { read.Close() })
	if err := db.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.UserCanvas{}, &entity.UserCanvasVersion{}); err != nil {
		t.Fatal(err)
	}
	before := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = before })
	owner := "owner"
	j.Event("reserve-public-agent-owner-before-insert", map[string]any{"user_id": owner, "tenant_id": owner})
	if err := db.Create(&entity.User{ID: owner, Nickname: owner, Email: "wire@example.invalid"}).Error; err != nil {
		t.Fatal(err)
	}
	status := "1"
	if err := db.Create(&entity.Tenant{ID: owner, Status: &status}).Error; err != nil {
		t.Fatal(err)
	}
	if err := redisengine.Init(&rc); err != nil {
		t.Fatal("required Redis unavailable")
	}
	client := redisengine.Get().GetClient()
	t.Cleanup(func() { redisengine.Close() })
	redisRead := redis.NewClient(&redis.Options{Addr: net.JoinHostPort(rc.Host, fmt.Sprint(rc.Port)), Password: rc.Password, DB: rc.DB})
	t.Cleanup(func() { redisRead.Close() })
	hook := &agentWireRedisJournal{j: j, read: redisRead, keys: map[string]bool{}}
	client.AddHook(hook)
	t.Cleanup(func() {
		hook.mu.Lock()
		keys := []string{}
		for key := range hook.keys {
			keys = append(keys, key)
		}
		hook.mu.Unlock()
		sort.Strings(keys)
		if len(keys) == 0 {
			t.Error("public HTTP run did not register any Redis run keys")
		}
		ctx, done := context.WithTimeout(context.Background(), 5*time.Second)
		defer done()
		if len(keys) > 0 {
			if client.Del(ctx, keys...).Err() != nil {
				t.Error("owned Redis cleanup failed")
			}
			n, err := redisRead.Exists(ctx, keys...).Result()
			j.Event("public-agent-redis-cleanup-readback", map[string]any{"keys": keys, "absent": err == nil && n == 0})
			if err != nil || n != 0 {
				t.Error("owned Redis keys survived")
			}
		}
	})
	gin.SetMode(gin.TestMode)
	engine := gin.New()
	engine.Use(func(c *gin.Context) {
		if c.GetHeader("X-Test-Actor") == owner {
			c.Set("user", &entity.User{ID: owner})
		}
		c.Next()
	})
	router.RegisterAgentExecutionRoutes(engine.Group("/api/v1"), handler.NewAgentHandler(service.NewAgentService()))
	web := testsupport.NewHTTP(t, engine)
	request := func(method, path, body string) (int, []byte) {
		t.Helper()
		req, err := http.NewRequestWithContext(t.Context(), method, web.URL+"/api/v1"+path, bytes.NewBufferString(body))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("X-Test-Actor", owner)
		req.Header.Set("Content-Type", "application/json")
		resp, err := web.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		raw, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
		if err != nil {
			t.Fatal(err)
		}
		return resp.StatusCode, raw
	}
	dsl := `{"components":{"begin":{"obj":{"component_name":"Begin","params":{}},"downstream":["message"],"upstream":[]},"message":{"obj":{"component_name":"Message","params":{"text":"Echo {{sys.query}}"}},"downstream":[],"upstream":["begin"]}}}`
	j.Event("reserve-public-agent-create-in-owned-table", map[string]any{"database": name, "table": "t_ai_user_canvases", "owner_id": owner})
	_, raw := request("POST", "/agents", `{"title":"wire","dsl":`+dsl+`}`)
	var created struct {
		Code common.ErrorCode `json:"code"`
		Data struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if json.Unmarshal(raw, &created) != nil || created.Code != common.CodeSuccess || created.Data.ID == "" {
		t.Fatalf("create HTTP: %s", raw)
	}
	id := created.Data.ID
	var stored string
	if err := read.QueryRow("SELECT title FROM t_ai_user_canvases WHERE id=$1 AND user_id=$2", id, owner).Scan(&stored); err != nil || stored != "wire" {
		t.Fatal("independent HTTP create readback", err)
	}
	_, raw = request("POST", "/agents/"+id+"/run", `{"query":"wire"}`)
	var runID string
	messages, finished := 0, false
	for _, line := range strings.Split(string(raw), "\n") {
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		var event service.AgentRunEvent
		if json.Unmarshal([]byte(strings.TrimPrefix(line, "data: ")), &event) != nil {
			t.Fatal("invalid SSE")
		}
		if event.RunID == "" || !regexp.MustCompile(`^[0-9a-f]{32}$`).MatchString(event.RunID) {
			t.Fatal("invalid SSE run identity")
		}
		if runID == "" {
			runID = event.RunID
			j.Event("public-agent-http-run-observed", map[string]any{"canvas_id": id, "run_id": runID, "database": name, "owner_id": owner})
		} else if runID != event.RunID {
			t.Fatal("SSE run identity changed")
		}
		if event.Event == "message" {
			messages++
			if event.Data["content"] != "Echo wire" {
				t.Fatal(event)
			}
		}
		if event.Event == "workflow_finished" {
			finished = true
		}
		if event.Event == "error" {
			t.Fatal(event)
		}
	}
	if runID == "" || messages != 1 || !finished {
		t.Fatalf("real HTTP SSE missing output: %s", raw)
	}
	registered, err := hook.registeredRunKeys(runID)
	if err != nil {
		t.Fatal(err)
	}
	j.Event("public-agent-http-run-reservations-verified", map[string]any{"canvas_id": id, "run_id": runID, "keys": registered})
	persisted, err := redisRead.Get(t.Context(), "agent:execution:"+runID).Bytes()
	if err != nil {
		t.Fatal(err)
	}
	var record struct {
		Status   string `json:"status"`
		ActorID  string `json:"actor_id"`
		CanvasID string `json:"canvas_id"`
	}
	if json.Unmarshal(persisted, &record) != nil || record.Status != "succeeded" || record.ActorID != owner || record.CanvasID != id {
		t.Fatal("independent HTTP Redis readback")
	}
	if n, err := redisRead.Exists(t.Context(), "agent:cp:"+runID, "agent:execution:"+runID+":lock").Result(); err != nil || n != 0 {
		t.Fatal("HTTP terminal resources survived")
	}
	_, raw = request("POST", "/agents/"+id+"/run?version=unknown", `{}`)
	var rejected struct {
		Code common.ErrorCode `json:"code"`
	}
	if json.Unmarshal(raw, &rejected) != nil || rejected.Code != common.CodeArgumentError {
		t.Fatalf("unsupported query: %s", raw)
	}
}

// go-redis encodes a nonzero-TTL SetNX as SET key value [PX|EX ttl] NX.
// Look only at command options; a value or expiry argument spelled NX does
// not make an ordinary SET into a lock claim.
func agentWireLockClaim(cmd redis.Cmder) (string, bool) {
	args := cmd.Args()
	if len(args) < 3 {
		return "", false
	}
	key, ok := args[1].(string)
	if !ok {
		return "", false
	}
	switch strings.ToLower(cmd.Name()) {
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

func agentWireRunKeys(runID string) []string {
	return []string{"agent:execution:" + runID, "agent:execution:" + runID + ":cancel", "agent:execution:" + runID + ":lock", "agent:cp:" + runID}
}

func (h *agentWireRedisJournal) registeredRunKeys(runID string) ([]string, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	expected := agentWireRunKeys(runID)
	for _, key := range expected {
		if !h.keys[key] {
			return nil, fmt.Errorf("public run %s has an unregistered key %s", runID, key)
		}
	}
	if len(h.keys) < 4 {
		return nil, fmt.Errorf("no complete public run key reservation")
	}
	return expected, nil
}

type agentWireFakeJournal struct{ events []map[string]any }

func (j *agentWireFakeJournal) Event(event string, fields map[string]any) {
	value := map[string]any{"event": event}
	for key, item := range fields {
		value[key] = item
	}
	j.events = append(j.events, value)
}

type agentWireFakeRedisReader struct {
	calls int
	keys  []string
	count int64
	err   error
}

func (r *agentWireFakeRedisReader) Exists(ctx context.Context, keys ...string) *redis.IntCmd {
	r.calls++
	r.keys = append([]string(nil), keys...)
	result := redis.NewIntCmd(ctx, "exists")
	result.SetVal(r.count)
	result.SetErr(r.err)
	return result
}

func TestAgentWireRedisJournalReservesActualSetNXBeforeNext(t *testing.T) {
	const runID = "0123456789abcdef0123456789abcdef"
	key := "agent:execution:" + runID + ":lock"
	cases := []struct {
		name    string
		args    []any
		reserve bool
	}{
		{"actual SET PX NX", []any{"set", key, "token", "px", int64((6 * time.Minute).Milliseconds()), "nx"}, true},
		{"actual SET EX NX", []any{"SET", key, "token", "EX", int64(360), "NX"}, true},
		{"SET NX before expiry", []any{"set", key, "token", "NX", "PX", int64(1000)}, true},
		{"legacy SETNX", []any{"setnx", key, "token"}, true},
		{"ordinary SET", []any{"set", key, "token", "PX", int64(1000)}, false},
		{"NX value is not option", []any{"set", key, "NX"}, false},
		{"NX expiry argument is not option", []any{"set", key, "token", "PX", "NX"}, false},
		{"ordinary SET GET", []any{"set", key, "token", "GET"}, false},
		{"XX is not NX", []any{"set", key, "token", "EX", int64(60), "XX"}, false},
		{"unrelated namespace", []any{"set", "other:lock", "token", "NX"}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			journal := &agentWireFakeJournal{}
			read := &agentWireFakeRedisReader{}
			hook := &agentWireRedisJournal{j: journal, read: read, keys: map[string]bool{}}
			nextCalled := false
			sentinel := errors.New("next sentinel")
			next := func(_ context.Context, actual redis.Cmder) error {
				nextCalled = true
				if actual.Args()[1] != tc.args[1] {
					t.Fatal("hook changed business command")
				}
				if tc.reserve {
					if read.calls != 1 || len(journal.events) != 1 {
						t.Fatalf("next reached before reservation/probe: %d %+v", read.calls, journal.events)
					}
					if journal.events[0]["event"] != "reserve-public-agent-run-before-redis-write" || journal.events[0]["run_id"] != runID {
						t.Fatal("wrong reservation event")
					}
					if _, err := hook.registeredRunKeys(runID); err != nil {
						t.Fatal(err)
					}
					expectedKeys := []string{"agent:execution:" + runID, "agent:execution:" + runID + ":cancel", "agent:execution:" + runID + ":lock", "agent:cp:" + runID}
					registered, ok := journal.events[0]["keys"].([]string)
					if !ok || strings.Join(registered, "|") != strings.Join(expectedKeys, "|") || strings.Join(read.keys, "|") != strings.Join(expectedKeys, "|") {
						t.Fatalf("wrong four-key reservation/probe before next: registered=%v probed=%v", registered, read.keys)
					}
				} else if read.calls != 0 || len(journal.events) != 0 || len(hook.keys) != 0 {
					t.Fatal("ordinary SET was incorrectly treated as run allocation")
				}
				return sentinel
			}
			err := hook.ProcessHook(next)(context.Background(), redis.NewStatusCmd(context.Background(), tc.args...))
			if !nextCalled || !errors.Is(err, sentinel) {
				t.Fatalf("next result not preserved: %v", err)
			}
		})
	}
}

func TestAgentWireRedisJournalRefusesOccupiedRunBeforeNext(t *testing.T) {
	const runID = "0123456789abcdef0123456789abcdef"
	for _, read := range []*agentWireFakeRedisReader{{count: 1}, {err: errors.New("probe unavailable")}} {
		hook := &agentWireRedisJournal{j: &agentWireFakeJournal{}, read: read, keys: map[string]bool{}}
		nextCalled := false
		err := hook.ProcessHook(func(context.Context, redis.Cmder) error { nextCalled = true; return nil })(context.Background(), redis.NewStatusCmd(context.Background(), "set", "agent:execution:"+runID+":lock", "token", "PX", int64(360000), "NX"))
		if err == nil || nextCalled || len(hook.keys) != 0 {
			t.Fatalf("failed reservation reached write: %v next=%v", err, nextCalled)
		}
	}
}
