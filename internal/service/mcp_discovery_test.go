package service

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/testsupport/scratch"
	"testing"
)

func TestMCPDiscoveryValidation(t *testing.T) {
	s := &MCPService{}
	for _, request := range []*TestServerRequest{{URL: "http://127.0.0.1/private", ServerType: "sse"}, {URL: "https://example.com", ServerType: "sse", Headers: map[string]interface{}{"Authorization": nil}}, {URL: "https://example.com", ServerType: "sse", Timeout: math.Inf(1)}, {URL: "https://example.com", ServerType: "sse", Timeout: math.NaN()}, {URL: "https://example.com", ServerType: "sse", Timeout: 10000}} {
		if tools, err := s.TestServer(t.Context(), "id", request); err == nil || tools != nil {
			t.Fatal("unsafe MCP request accepted")
		}
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if tools, err := s.TestServer(ctx, "id", &TestServerRequest{URL: "https://example.com", ServerType: "sse"}); tools != nil || !errors.Is(err, context.Canceled) {
		t.Fatal("cancelled discovery reached transport", err)
	}
	if result, err := s.ImportServers(ctx, "owner", map[string]map[string]interface{}{"server": {"type": "sse", "url": "https://example.com"}}, 0); result != nil || !errors.Is(err, context.Canceled) {
		t.Fatal("cancelled import reached SQL", err)
	}
	for _, raw := range []json.RawMessage{json.RawMessage(`[]`), json.RawMessage(`{"X":`), json.RawMessage(`"not an object"`)} {
		if _, err := safeJSONMap(raw); err == nil {
			t.Error("malformed MCP object accepted")
		}
	}
}
func TestMCPDiscoverySQL(t *testing.T) {
	f := scratch.New(t)
	old := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = old })
	if err := f.DB.AutoMigrate(&entity.Tenant{}, &entity.MCPServer{}); err != nil {
		t.Fatal(err)
	}
	owner, other := f.Prefix+"owner", f.Prefix+"other"
	active := "1"
	for _, id := range []string{owner, other} {
		if err := f.DB.Create(&entity.Tenant{ID: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	s := NewMCPService()
	created, code, err := s.CreateMCPServer(owner, CreateMCPServerRequest{Name: "Controlled", URL: "https://example.invalid/mcp", ServerType: "streamable-http", Headers: json.RawMessage(`{"Authorization":""}`), Variables: json.RawMessage(`{"zero":0,"flag":false,"tools":{"untrusted":"ignored"}}`)})
	if err != nil || code != common.CodeSuccess {
		t.Fatal("canonical MCP create", code, err)
	}
	var raw string
	if err := f.Read.QueryRow("SELECT variables FROM mcp_server WHERE id=? AND tenant_id=?", created.ID, owner).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var fields map[string]interface{}
	if err := json.Unmarshal([]byte(raw), &fields); err != nil || fields["zero"] != float64(0) || fields["flag"] != false || len(fields["tools"].(map[string]interface{})) != 0 {
		t.Fatal("MCP persisted variables/reset tools", err)
	}
	if result, _, err := s.UpdateMCPServer(other, created.ID, UpdateMCPServerRequest{"url": json.RawMessage(`"https://foreign.invalid"`)}); err == nil || result != nil {
		t.Fatal("foreign MCP update accepted")
	}
	if _, _, err := s.CreateMCPServer(owner, CreateMCPServerRequest{Name: "Malformed", URL: "https://example.invalid/mcp", ServerType: "sse", Headers: json.RawMessage(`{"Authorization":null}`)}); err == nil {
		t.Fatal("invalid header persisted")
	}
	results, err := s.ImportServers(t.Context(), owner, map[string]map[string]interface{}{"unsafe": {"type": "sse", "url": "http://127.0.0.1/private"}, "wrong-type": {"type": "stdio", "url": "https://example.invalid"}}, 1)
	if err != nil || len(results) != 2 {
		t.Fatal(results, err)
	}
	for _, result := range results {
		if result.Success {
			t.Fatal("unsafe import succeeded")
		}
	}
	var rows int
	if err := f.Read.QueryRow("SELECT count(*) FROM mcp_server").Scan(&rows); err != nil || rows != 1 {
		t.Fatal("rejected discovery changed SQL", rows, err)
	}
	f.Journal.Event("canonical-mcp-sql-readback", map[string]any{"persisted_servers": rows, "foreign_update_denied": true, "unsafe_import_writes": 0})
}
