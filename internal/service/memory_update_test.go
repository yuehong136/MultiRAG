package service

import (
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/testsupport/scratch"
	"testing"
)

func TestMemoryUpdateDiffPreservesExplicitValues(t *testing.T) {
	f := scratch.New(t)
	old := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = old })
	if err := f.DB.AutoMigrate(&entity.Memory{}); err != nil {
		t.Fatal(err)
	}
	memory := &entity.Memory{ID: "memory", Name: "memory", TenantID: "owner", Permissions: "me", LLMID: "llm", EmbdID: "embd", StorageType: "table", ForgettingPolicy: "FIFO", MemorySize: 1, Temperature: 0.5, MemoryType: dao.CalculateMemoryType([]string{"raw", "semantic"})}
	if err := f.DB.Create(memory).Error; err != nil {
		t.Fatal(err)
	}
	zero := 0.0
	empty := ""
	size := int64(1)
	s := NewMemoryService()
	request := &UpdateMemoryRequest{Temperature: &zero, Description: &empty, Avatar: &empty, MemorySize: &size}
	if _, err := s.UpdateMemory("owner", "memory", request); err != nil {
		t.Fatal(err)
	}
	var temperature float64
	var description, avatar string
	var actualSize int64
	if err := f.Read.QueryRow("SELECT temperature,description,avatar,memory_size FROM memory WHERE id=?", "memory").Scan(&temperature, &description, &avatar, &actualSize); err != nil || temperature != 0 || description != "" || avatar != "" || actualSize != 1 {
		t.Fatal("explicit zero/empty fields omitted", err)
	}
	if _, err := s.UpdateMemory("foreign", "memory", &UpdateMemoryRequest{Temperature: &zero}); err == nil {
		t.Fatal("foreign memory update/read accepted")
	}
	if _, err := s.UpdateMemory("owner", "memory", request); err != nil {
		t.Fatal("same explicit values should remain idempotent", err)
	}
}
