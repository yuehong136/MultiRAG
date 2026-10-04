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

func TestMoonshotConfiguredDriverAndThinkingDefaults(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	provider := manager.FindProvider("Moonshot")
	if provider == nil || provider.ModelDriver.Name() != "moonshot" || provider.URL["default"] != "https://api.moonshot.cn/v1" || provider.URLSuffix.Chat != "chat/completions" {
		t.Fatal("Moonshot configuration or registration is missing")
	}
	explicitFalse := false
	for _, test := range []struct {
		name     string
		explicit *bool
		want     string
	}{
		{"kimi-k2.6", nil, "enabled"},
		{"kimi-k2.5", &explicitFalse, "disabled"},
		{"moonshot-v1-8k", nil, ""},
	} {
		t.Run(test.name, func(t *testing.T) {
			model, err := manager.GetModelByName(provider.Name, test.name)
			if err != nil || model.Class == nil || *model.Class != "kimi" || !model.ModelTypeMap["chat"] {
				t.Fatalf("model = %#v, %v", model, err)
			}
			config := &models.ChatConfig{Thinking: test.explicit}
			applyModelChatDefaults(model, config)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body struct {
					Thinking *struct{ Type string }
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if test.want == "" {
					if body.Thinking != nil {
						t.Error("thinking enabled for plain model")
					}
				} else if body.Thinking == nil || body.Thinking.Type != test.want {
					t.Errorf("thinking = %#v; want %s", body.Thinking, test.want)
				}
				fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
			}))
			defer server.Close()
			driver, err := models.NewModelFactory().CreateModelDriver(provider.Name, map[string]string{"default": server.URL}, provider.URLSuffix)
			if err != nil {
				t.Fatal(err)
			}
			key, message := "key", "question"
			if _, err := driver.ChatWithMessages(test.name, &models.APIConfig{APIKey: &key}, []models.Message{{Role: "user", Content: message}}, config); err != nil {
				t.Fatal(err)
			}
		})
	}
}
