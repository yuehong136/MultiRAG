package service

import (
	"multirag/internal/dao"
	"multirag/internal/entity"
	"testing"
)

func TestMemoryUpdateDiffPreservesExplicitValues(t *testing.T) {
	zero := int64(0)
	empty := ""
	current := &entity.Memory{Name: "memory", Permissions: "me", LLMID: "llm", EmbdID: "embd", MemoryType: dao.CalculateMemoryType([]string{"raw", "semantic"}), TenantLLMID: &zero, Temperature: 0, Avatar: &empty, ForgettingPolicy: "FIFO"}
	same := map[string]any{"name": "memory", "permissions": "me", "llm_id": "llm", "embd_id": "embd", "tenant_llm_id": zero, "temperature": float64(0), "avatar": "", "forgetting_policy": "FIFO", "memory_type": []string{"semantic", "raw", "raw"}}
	if got := memoryUpdateDiff(current, same); len(got) != 0 {
		t.Fatal("same values changed memory", got)
	}
	for field, value := range map[string]any{"tenant_embd_id": zero, "description": "", "memory_size": int64(1), "temperature": float64(0.5)} {
		if got := memoryUpdateDiff(current, map[string]any{field: value}); len(got) != 1 {
			t.Fatal("explicit value omitted", field, got)
		}
	}
}
