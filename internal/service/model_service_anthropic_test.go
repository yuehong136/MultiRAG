package service

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

func TestAnthropicCatalogBoundChatHTTP(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	provider := manager.FindProvider("Anthropic")
	if provider == nil || provider.ModelDriver.Name() != "anthropic" || provider.URLSuffix.Chat != "v1/messages" || len(provider.Models) != 9 {
		t.Fatal("Anthropic catalog/factory missing")
	}
	for _, model := range provider.Models {
		if !model.ModelTypeMap["chat"] || !model.ModelTypeMap["vision"] {
			t.Fatal("catalog capability lost")
		}
	}
	original := provider.URL["default"]
	for _, instance := range []string{"one", "two"} {
		t.Run(instance, func(t *testing.T) {
			t.Parallel()
			calls := 0
			fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				var body map[string]any
				json.NewDecoder(r.Body).Decode(&body)
				if r.URL.Path != "/instance/v1/messages" || r.Header.Get("x-api-key") != instance || body["system"] != "rules" {
					t.Error("native instance binding lost", body)
				}
				fmt.Fprintf(w, `{"content":[{"type":"thinking","thinking":"reason"},{"type":"text","text":%q}]}`, instance)
			}))
			defer fixture.Close()
			extra, err := encodeProviderInstanceExtra("office", fixture.URL+"/instance/")
			if err != nil {
				t.Fatal(err)
			}
			driver, api, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", APIKey: instance, Extra: extra})
			if err != nil {
				t.Fatal(err)
			}
			name := "claude-sonnet-4-5-20250929"
			bound := models.NewChatModel(driver, &name, api)
			messages := []models.Message{{Role: "system", Content: "rules"}, {Role: "user", Content: "q"}}
			answer, err := bound.ChatWithMessages(messages, nil)
			if err != nil || answer == nil || *answer.Answer != instance || *answer.ReasoningContent != "reason" {
				t.Fatal(answer, err)
			}
			if err := bound.ChatStreamlyWithMessages(messages, nil, func(*string, *string) error { return nil }); err == nil || calls != 1 {
				t.Fatal("unsupported stream made request", err, calls)
			}
			if provider.URL["default"] != original {
				t.Fatal("catalog mutated")
			}
		})
	}
}
