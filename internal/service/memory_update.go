package service

import (
	"reflect"
	"strings"

	"multirag/internal/dao"
	"multirag/internal/entity"
)

// memoryUpdateDiff compares validated fields before mutation restrictions and
// prompt regeneration. Nil pointers remain distinct from explicit zero/empty values.
func memoryUpdateDiff(memory *entity.Memory, updates map[string]any) map[string]any {
	current := map[string]any{
		"name": strings.TrimSpace(memory.Name), "permissions": strings.ToLower(strings.TrimSpace(memory.Permissions)),
		"llm_id": strings.TrimSpace(memory.LLMID), "embd_id": strings.TrimSpace(memory.EmbdID),
		"memory_size": memory.MemorySize, "forgetting_policy": strings.ToUpper(strings.TrimSpace(memory.ForgettingPolicy)), "temperature": memory.Temperature,
	}
	for key, value := range map[string]*int64{"tenant_llm_id": memory.TenantLLMID, "tenant_embd_id": memory.TenantEmbdID} {
		if value != nil {
			current[key] = *value
		} else {
			current[key] = nil
		}
	}
	for key, value := range map[string]*string{"avatar": memory.Avatar, "description": memory.Description, "system_prompt": memory.SystemPrompt, "user_prompt": memory.UserPrompt} {
		if value != nil {
			current[key] = *value
		} else {
			current[key] = nil
		}
	}
	result := make(map[string]any, len(updates))
	for field, value := range updates {
		if field == "memory_type" {
			if types, ok := value.([]string); ok && dao.CalculateMemoryType(types) == memory.MemoryType {
				continue
			}
		} else if stored, ok := current[field]; ok && reflect.DeepEqual(stored, value) {
			continue
		}
		result[field] = value
	}
	return result
}
