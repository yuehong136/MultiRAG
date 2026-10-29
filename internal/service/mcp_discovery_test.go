package service

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/utility"
)

func TestMCPDiscoveryValidation(t *testing.T) {
	for _, raw := range []string{`{"headers":{"Authorization":null}}`, `{"headers":{"X":1}}`, `{"headers":{"X":"one","X":"two"}}`} {
		var req MCPDiscoveryRequest
		if json.Unmarshal([]byte(raw), &req) == nil {
			t.Fatal("invalid header accepted", raw)
		}
	}
	var empty MCPDiscoveryRequest
	if json.Unmarshal([]byte(`{"headers":{"X":""}}`), &empty) != nil || empty.Headers["X"] != "" {
		t.Fatal("explicit empty string rejected")
	}

	vars, err := mcpStringMap(map[string]any{"zero": float64(0), "flag": false, "object": map[string]any{}, "tools": map[string]any{}}, true)
	if err != nil || vars["zero"] != "0" || vars["flag"] != "False" || len(vars) != 2 {
		t.Fatal("JSON scalar template values lost", vars, err)
	}
	for _, value := range []float64{-1, 61, 10000} {
		if _, err := mcpDiscoveryTimeout(value); err == nil {
			t.Fatal("unbounded timeout accepted")
		}
	}
	for _, raw := range []string{`null`, `{"type":"stdio","url":"https://example.com"}`, `{"type":"sse","url":"https://example.com","headers":{"X":1}}`} {
		if _, _, err := decodeMCPImport("name", json.RawMessage(raw), 10); err == nil {
			t.Fatal("invalid config accepted", raw)
		}
	}
	if _, _, err := decodeMCPImport("name", json.RawMessage(`{"type":"sse","url":"https://example.com","headers":{"Authorization":"${token}"},"token":"value"}`), 0); err != nil {
		t.Fatal(err)
	}
}

// A true scratch PostgreSQL transaction/readback test. Remote discovery is the
// explicit injected boundary; utility tests exercise real HTTP/SSE transports.
func TestMCPDiscoverySQL(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_MCP_POSTGRES_DSN")
	if dsn == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned MCP scratch database required")
		}
		t.Skip("requires owned MCP scratch")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Discard})
	if err != nil {
		t.Fatal("scratch connection failed")
	}
	pool, _ := db.DB()
	defer pool.Close()
	read, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer read.Close()
	var dbname string
	if err := read.QueryRow("SELECT current_database()").Scan(&dbname); err != nil || dbname != "multirag_after98_s_20261029" {
		t.Fatal("unowned database")
	}
	previous := dao.DB
	dao.DB = db
	defer func() { dao.DB = previous }()
	if err := db.AutoMigrate(&entity.Tenant{}, &entity.User{}, &entity.UserTenant{}, &entity.MCPServer{}); err != nil {
		t.Fatal(err)
	}
	active := "1"
	for _, id := range []string{"s-owner", "s-other", "s-inactive", "s-member"} {
		if err := db.Create(&entity.User{ID: id, Email: id + "@test.invalid", Nickname: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Create(&entity.Tenant{ID: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		role := "owner"
		if id == "s-member" {
			role = "normal"
		}
		if err := db.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: id, Role: role, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := db.Model(&entity.Tenant{}).Where("id = ?", "s-inactive").Update("status", "0").Error; err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	calls := 0
	var lastTimeout time.Duration
	s := NewMCPService()
	s.fetchMCPTools = func(ctx context.Context, opts utility.FetchOptions) ([]utility.Tool, error) {
		mu.Lock()
		calls++
		lastTimeout = opts.Timeout
		mu.Unlock()
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		if strings.Contains(opts.URL, "fail") {
			return nil, errors.New("secret endpoint response")
		}
		return []utility.Tool{{Name: "search", Description: "Search", InputSchema: map[string]any{"type": "object"}}}, nil
	}
	count := func() int {
		var n int
		if err := read.QueryRow("SELECT count(*) FROM t_ai_mcp_server").Scan(&n); err != nil {
			t.Fatal(err)
		}
		return n
	}
	req := MCPDiscoveryRequest{URL: "https://example.com/mcp", ServerType: "streamable-http", Headers: map[string]string{"Authorization": "secret"}}
	before := count()
	mu.Lock()
	beforeInvalidCalls := calls
	mu.Unlock()
	invalidResults, invalidCode, invalidErr := s.ImportMCPServers(t.Context(), "s-owner", ImportMCPRequest{Servers: map[string]json.RawMessage{"null-header": json.RawMessage(`{"type":"sse","url":"https://example.com","headers":{"Authorization":null}}`)}})
	mu.Lock()
	afterInvalidCalls := calls
	mu.Unlock()
	if invalidErr != nil || invalidCode != common.CodeSuccess || len(invalidResults) != 1 || invalidResults[0].Success || count() != before || afterInvalidCalls != beforeInvalidCalls {
		t.Fatal("null header reached remote or SQL", invalidResults, invalidCode, invalidErr)
	}
	if _, code, err := s.TestMCPServer(t.Context(), "s-owner", req); err != nil || code != common.CodeSuccess || count() != before {
		t.Fatal("preview mutated database", code, err)
	}
	for _, id := range []string{"s-member", "s-inactive", "missing"} {
		mu.Lock()
		beforeCalls := calls
		mu.Unlock()
		if _, code, err := s.TestMCPServer(t.Context(), id, req); err == nil || code != common.CodeForbidden {
			t.Fatal("unauthorized preview", id, code)
		}
		mu.Lock()
		if calls != beforeCalls {
			t.Fatal("denied preview reached remote")
		}
		mu.Unlock()
	}
	imported := ImportMCPRequest{Servers: map[string]json.RawMessage{"good": json.RawMessage(`{"type":"sse","url":"https://example.com","authorization_token":"secret","tool_configuration":{"custom":true},"zero":0}`), "bad": json.RawMessage(`{"type":"sse","url":"https://fail.example.com"}`)}}
	results, code, err := s.ImportMCPServers(t.Context(), "s-owner", imported)
	if err != nil || code != common.CodeSuccess || len(results) != 2 || count() != before+1 {
		t.Fatal("per-entry atomic import", results, code, err)
	}
	if results[0].Success || strings.Contains(results[0].Message, "secret") || !results[1].Success {
		t.Fatal("failure semantics", results)
	}
	id := results[1].ID
	if _, code, err := s.GetMCPServerTools(t.Context(), "s-other", id, 0); err == nil || code != common.CodeForbidden {
		t.Fatal("foreign discovery permitted")
	}
	if _, err := read.Exec("UPDATE t_ai_mcp_server SET variables=jsonb_set(variables,'{tools,search,enabled}','false'::jsonb) WHERE id=$1", id); err != nil {
		t.Fatal(err)
	}
	if tools, code, err := s.GetMCPServerTools(t.Context(), "s-owner", id, 2.5); err != nil || code != common.CodeSuccess || len(tools) != 1 || tools[0]["enabled"] != false {
		t.Fatal("owned discovery or disabled tool state failed", code, err)
	}

	mu.Lock()
	if lastTimeout != 2500*time.Millisecond {
		t.Fatal("GET tools timeout ignored")
	}
	mu.Unlock()
	// Simultaneous imports share the tenant lock and choose distinct names.
	var wg sync.WaitGroup
	errs := make(chan error, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r, _, e := s.ImportMCPServers(t.Context(), "s-owner", ImportMCPRequest{Servers: map[string]json.RawMessage{"good": imported.Servers["good"]}})
			if e != nil || len(r) != 1 || !r[0].Success {
				errs <- errors.New("concurrent import failed")
			}
		}()
	}
	wg.Wait()
	close(errs)
	for e := range errs {
		t.Error(e)
	}
	var names string
	if err := read.QueryRow("SELECT string_agg(name,',' ORDER BY name) FROM t_ai_mcp_server WHERE tenant_id='s-owner'").Scan(&names); err != nil || names != "good,good_0,good_1" {
		t.Fatal("name collision serialization", names, err)
	}
	// Commit failure must roll back all columns in that entry.
	if _, err := read.Exec("CREATE FUNCTION fail_mcp_insert() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'forced'; END $$; CREATE TRIGGER fail_mcp BEFORE INSERT ON t_ai_mcp_server FOR EACH ROW EXECUTE FUNCTION fail_mcp_insert()"); err != nil {
		t.Fatal(err)
	}
	before = count()
	r, _, err := s.ImportMCPServers(t.Context(), "s-owner", ImportMCPRequest{Servers: map[string]json.RawMessage{"rollback": imported.Servers["good"]}})
	if err != nil || len(r) != 1 || r[0].Success || count() != before {
		t.Fatal("failed insert left data", r, err)
	}
	if _, err := read.Exec("DROP TRIGGER fail_mcp ON t_ai_mcp_server; DROP FUNCTION fail_mcp_insert()"); err != nil {
		t.Fatal(err)
	}
	cancelled, cancel := context.WithCancel(t.Context())
	cancel()
	if _, _, err := s.ImportMCPServers(cancelled, "s-owner", imported); err == nil || count() != before {
		t.Fatal("cancelled import mutated database")
	}
	originalFetch := s.fetchMCPTools
	two := ImportMCPRequest{Servers: map[string]json.RawMessage{"a": imported.Servers["good"], "b": imported.Servers["good"]}}
	inFlight, cancelInFlight := context.WithCancel(t.Context())
	defer cancelInFlight()
	scheduled := 0
	s.fetchMCPTools = func(ctx context.Context, opts utility.FetchOptions) ([]utility.Tool, error) {
		scheduled++
		cancelInFlight()
		return nil, ctx.Err()
	}
	if _, code, err := s.ImportMCPServers(inFlight, "s-owner", two); err == nil || code == common.CodeSuccess || scheduled != 1 || count() != before {
		t.Fatal("cancellation scheduled subsequent entry", code, err, scheduled)
	}
	scheduled = 0
	s.fetchMCPTools = func(ctx context.Context, opts utility.FetchOptions) ([]utility.Tool, error) {
		scheduled++
		if err := db.Model(&entity.UserTenant{}).Where("id = ?", "s-owner").Update("role", "normal").Error; err != nil {
			return nil, err
		}
		return []utility.Tool{{Name: "search", InputSchema: map[string]any{"type": "object"}}}, nil
	}
	if _, code, err := s.ImportMCPServers(t.Context(), "s-owner", two); err == nil || code != common.CodeForbidden || scheduled != 1 || count() != before {
		t.Fatal("revocation scheduled or persisted entry", code, err, scheduled)
	}
	if err := db.Model(&entity.UserTenant{}).Where("id = ?", "s-owner").Update("role", "owner").Error; err != nil {
		t.Fatal(err)
	}
	s.fetchMCPTools = originalFetch
	t.Log("preview read-only; inactive/member/foreign denial; per-entry failure atomicity; serialized duplicate rename; disabled tools and arbitrary JSON variables; rollback and cancellation/revocation independently read back")
}
