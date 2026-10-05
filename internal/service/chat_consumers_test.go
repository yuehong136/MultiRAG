package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/logger"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestTenantDefaultModelNames(t *testing.T) {
	tenant := &entity.Tenant{LLMID: "model@provider"}
	if got, err := tenantDefaultModelName(tenant, entity.ModelTypeChat); err != nil || got != tenant.LLMID {
		t.Fatal(got, err)
	}
	if got, err := tenantDefaultModelName(tenant, entity.ModelTypeTTS); err != nil || got != "" {
		t.Fatal(got, err)
	}
	if _, err := tenantDefaultModelName(tenant, "invalid"); err == nil {
		t.Fatal("invalid type accepted")
	}
}

func TestChatConsumersUseBoundInstanceAndContext(t *testing.T) {
	if err := logger.Init("error"); err != nil {
		t.Fatal(err)
	}
	answer := "keywords"
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Path != "/instance/chat/completions" || r.Header.Get("Authorization") != "Bearer local-key" {
			t.Error("binding lost")
		}
		var body struct {
			Messages    []models.Message
			Temperature float64
			Model       string
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if len(body.Messages) != 2 || body.Messages[0].Role != "system" || body.Messages[1].Role != "user" || body.Temperature != 0.25 || body.Model != "custom" {
			t.Errorf("model defaults/history lost %+v", body)
		}
		json.NewEncoder(w).Encode(map[string]any{"choices": []any{map[string]any{"message": map[string]any{"content": answer}}}})
	}))
	defer server.Close()
	name, key, temp := "custom", "local-key", 0.25
	model := models.NewChatModel(models.NewVLLMModel(map[string]string{"default": server.URL + "/instance"}, models.URLSuffix{Chat: "chat/completions"}), &name, &models.APIConfig{APIKey: &key})
	model.ModelConfig.Temperature = &temp
	if got, err := KeywordExtraction(context.Background(), model, "some text", 3); err != nil || got != "keywords" {
		t.Fatal(got, err)
	}
	answer = "Output: English === 中文"
	if got, err := CrossLanguages(context.Background(), model, "question", []string{"en", "zh"}); err != nil || got != "English\n中文" {
		t.Fatal(got, err)
	}
	answer = `{"conditions":[],"logic":"and"}`
	if got, err := GenMetaFilter(context.Background(), model, map[string]any{"author": map[string]any{"person": []string{"doc"}}}, "question", nil); err != nil || got.Logic != "and" {
		t.Fatal(got, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := KeywordExtraction(ctx, model, "text", 3); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancel lost: %v", err)
	}
	if calls != 3 || model.APIConfig.Context != nil {
		t.Fatal("request context mutated binding")
	}
	if _, err := chatWithContext(context.Background(), nil, nil); err == nil {
		t.Fatal(fmt.Errorf("nil model accepted"))
	}
}
