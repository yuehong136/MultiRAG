package service

import (
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/testsupport/scratch"
	"sync"
	"testing"
)

func TestCanonicalMCPConcurrentName(t *testing.T) {
	f := scratch.New(t)
	previous := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = previous })
	if err := f.DB.AutoMigrate(&entity.Tenant{}, &entity.MCPServer{}); err != nil {
		t.Fatal(err)
	}
	active := "1"
	if err := f.DB.Create(&entity.Tenant{ID: f.Prefix, Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	service := NewMCPService()
	var wg sync.WaitGroup
	results := make(chan bool, 8)
	for range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			row, code, err := service.CreateMCPServer(f.Prefix, CreateMCPServerRequest{Name: "unique name", URL: "https://fixture.invalid/mcp", ServerType: "sse"})
			results <- row != nil && code == common.CodeSuccess && err == nil
		}()
	}
	wg.Wait()
	close(results)
	successes := 0
	for ok := range results {
		if ok {
			successes++
		}
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM mcp_server WHERE tenant_id=? AND name=?", f.Prefix, "unique name").Scan(&count); err != nil || count != 1 || successes != 1 {
		t.Fatal("concurrent duplicate MCP server names", successes, count, err)
	}
	f.Journal.Event("canonical-mcp-name-uniqueness-readback", map[string]any{"concurrent_calls": 8, "successes": successes, "independent_sql_rows": count})
}
