package service

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

func TestJinaCatalogBoundChatHTTP(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	provider := manager.FindProvider("Jina")
	if provider == nil {
		t.Fatal("Jina catalog missing")
	}
	found := false
	for _, model := range provider.Models {
		if model.Name == "jina-vlm" && model.ModelTypeMap["chat"] && model.MaxTokens == 32768 {
			found = true
		}
	}
	if !found {
		t.Fatal("Jina chat catalog missing")
	}
	for _, instance := range []string{"one", "two"} {
		t.Run(instance, func(t *testing.T) {
			t.Parallel()
			fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/custom/chat/completions" || r.Header.Get("Authorization") != "Bearer "+instance {
					t.Error("instance binding lost")
				}
				fmt.Fprintf(w, `{"choices":[{"message":{"content":%q}}]}`, instance)
			}))
			defer fixture.Close()
			extra, err := encodeProviderInstanceExtra("deepsearch", fixture.URL+"/custom/")
			if err != nil {
				t.Fatal(err)
			}
			driver, api, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", APIKey: instance, Extra: extra})
			if err != nil {
				t.Fatal(err)
			}
			name := "jina-vlm"
			answer, err := models.NewChatModel(driver, &name, api).ChatWithMessages([]models.Message{{Role: "user", Content: "q"}}, nil)
			if err != nil || answer == nil || answer.Answer == nil || *answer.Answer != instance {
				t.Fatal(answer, err)
			}
		})
	}
}
