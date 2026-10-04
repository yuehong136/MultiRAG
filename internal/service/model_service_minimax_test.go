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

func TestMinimaxConfiguredDriverAndThinkingDefaults(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	provider := manager.FindProvider("MiniMax")
	if provider == nil || provider.ModelDriver.Name() != "minimax" || provider.URL["default"] != "https://api.minimaxi.com/" || provider.URLSuffix.Chat != "v1/text/chatcompletion_v2" {
		t.Fatal("Minimax configuration or registration is missing")
	}
	explicitFalse := false
	for _, test := range []struct {
		name     string
		explicit *bool
		want     string
	}{
		{"minimax-m2.7", nil, "adaptive"},
		{"minimax-m2.5", &explicitFalse, "disabled"},
	} {
		t.Run(test.name, func(t *testing.T) {
			model, err := manager.GetModelByName(provider.Name, test.name)
			if err != nil || model.Class == nil || *model.Class != "minimax" || !model.ModelTypeMap["chat"] {
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
			if _, err := driver.Chat(&test.name, &message, &models.APIConfig{APIKey: &key}, config); err != nil {
				t.Fatal(err)
			}
		})
	}
}
