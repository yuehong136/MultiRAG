package service

import (
	"multirag/internal/entity/models"
	"reflect"
	"testing"
)

func TestSessionModelMessagesPreserveMultimodalHistory(t *testing.T) {
	parts := []any{map[string]any{"type": "text", "text": "describe"}, map[string]any{"type": "image_url", "image_url": map[string]any{"url": "https://example.com/a.png"}}}
	messages := []map[string]any{{"role": "system", "content": "untrusted system"}, {"role": "assistant", "content": "previous", "reasoning_content": "thought"}, {"role": "user", "content": parts}}
	svc := &ChatSessionService{}
	got, err := svc.modelMessages(messages, "configured system")
	if err != nil || len(got) != 3 {
		t.Fatalf("history %#v %v", got, err)
	}
	if got[0].Content != "configured system" || got[1].ReasoningContent == nil || *got[1].ReasoningContent != "thought" || !reflect.DeepEqual(got[2].Content, parts) {
		t.Fatalf("lost data: %#v", got)
	}
	if err := models.ValidateMessages(got); err != nil {
		t.Fatal(err)
	}
	for _, bad := range []map[string]any{{"content": "x"}, {"role": "user", "content": 42}, {"role": "assistant", "content": "x", "reasoning_content": 42}} {
		if _, err := svc.modelMessages([]map[string]any{bad}, "rules"); err == nil {
			t.Fatalf("invalid message silently dropped: %#v", bad)
		}
	}
}
