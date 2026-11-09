package admin

import (
	"errors"
	"multirag/internal/entity"
	"multirag/internal/server"
	"net/http"
	"sync/atomic"
	"testing"
)

type afterFFNumericOutboundTrap struct{ calls atomic.Int64 }

func (t *afterFFNumericOutboundTrap) RoundTrip(*http.Request) (*http.Response, error) {
	t.calls.Add(1)
	return nil, errors.New("configuration contacted supplier")
}
func TestAfterFFAdminNumericModelConsumersSQL(t *testing.T) {
	f := newAdminFixture(t)
	cfg := server.GetConfig()
	cfg.UserDefaultLLM.DefaultModels.ChatModel = server.ModelConfig{Factory: "OpenAI", Name: "target-chat", APIKey: "chat-secret", BaseURL: "https://chat.invalid/v1"}
	cfg.UserDefaultLLM.DefaultModels.EmbeddingModel = server.ModelConfig{Factory: "OpenAI", Name: "target-embed", APIKey: "embed-secret", BaseURL: "https://embed.invalid/v1"}
	for _, m := range []entity.LLM{{LLMName: "target-chat", FID: "OpenAI", ModelType: "chat", Tags: "LLM", MaxTokens: 128}, {LLMName: "target-embed", FID: "OpenAI", ModelType: "embedding", Tags: "embedding", MaxTokens: 64}} {
		if err := f.DB.Create(&m).Error; err != nil {
			t.Fatal(err)
		}
	}
	trap := &afterFFNumericOutboundTrap{}
	previous := http.DefaultTransport
	http.DefaultTransport = trap
	t.Cleanup(func() { http.DefaultTransport = previous })
	result, err := f.service.CreateUser("models@fixture.invalid", "owned-password!", "user")
	if err != nil {
		t.Fatal(err)
	}
	id := result["id"].(string)
	var chat, embed string
	if err := f.Read.QueryRow("SELECT llm_id,embd_id FROM tenant WHERE id=?", id).Scan(&chat, &embed); err != nil || chat != "target-chat" || embed != "target-embed" {
		t.Fatal("target name defaults missing", err)
	}
	for _, want := range []struct {
		name, key, base string
		limit           int64
	}{{"target-chat", "chat-secret", "https://chat.invalid/v1", 128}, {"target-embed", "embed-secret", "https://embed.invalid/v1", 64}} {
		var tenant, key, base, status string
		var limit int64
		if err := f.Read.QueryRow("SELECT tenant_id,api_key,api_base,max_tokens,status FROM tenant_llm WHERE tenant_id=? AND llm_name=?", id, want.name).Scan(&tenant, &key, &base, &limit, &status); err != nil || tenant != id || key != want.key || base != want.base || limit != want.limit || status != "1" {
			t.Fatal("target catalog init mapping lost", want.name, err)
		}
	}
	if trap.calls.Load() != 0 {
		t.Fatal("registration called provider")
	}
}
