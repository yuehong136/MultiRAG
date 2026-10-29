package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"net/http"
	"sync/atomic"
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
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	var answer, reasoning atomic.Value
	answer.Store("keywords")
	reasoning.Store("")
	var calls atomic.Int64
	server := registeredModelServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
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
		json.NewEncoder(w).Encode(map[string]any{"choices": []any{map[string]any{"message": map[string]any{"content": answer.Load().(string), "reasoning_content": reasoning.Load().(string)}}}})
	}))
	name, key, temp := "custom", "local-key", 0.25
	model := models.NewChatModel(models.NewVLLMModel(map[string]string{"default": server.URL + "/instance"}, models.URLSuffix{Chat: "chat/completions"}), &name, &models.APIConfig{APIKey: &key})
	model.ModelConfig.Temperature = &temp
	if got, err := KeywordExtraction(context.Background(), model, "some text", 3); err != nil || got != "keywords" {
		t.Fatal(got, err)
	}
	answer.Store("Output: English === 中文")
	if got, err := CrossLanguages(context.Background(), model, "question", []string{"en", "zh"}); err != nil || got != "English\n中文" {
		t.Fatal(got, err)
	}
	answer.Store(`{"conditions":[],"logic":"and"}`)
	if got, err := GenMetaFilter(context.Background(), model, common.MetaData{"author": {"person": {"doc"}}}, "question", nil); err != nil || got.Logic != "and" {
		t.Fatal(got, err)
	}

	for _, bad := range []string{"**ERROR** unavailable", "<think>analysis</think>**ERROR** unavailable"} {
		answer.Store(bad)
		if got, err := KeywordExtraction(context.Background(), model, "text", 3); err == nil {
			t.Fatalf("keyword error marker became success: %q", got)
		}
		if got, err := CrossLanguages(context.Background(), model, "question", []string{"en"}); err == nil {
			t.Fatalf("translation error marker became success: %q", got)
		}
	}

	// The model driver already separates reasoning from the final answer. A
	// marker mentioned only in that reasoning is not a provider error result.
	answer.Store("English === 中文")
	reasoning.Store("**ERROR** is a word")
	if got, err := CrossLanguages(context.Background(), model, "question", []string{"en", "zh"}); err != nil || got != "English\n中文" {
		t.Fatal("reasoning text changed the visible translation", got, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := KeywordExtraction(ctx, model, "text", 3); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancel lost: %v", err)
	}
	if calls.Load() != 8 || model.APIConfig.Context != nil {
		t.Fatal("request context mutated binding")
	}
	if _, err := chatWithContext(context.Background(), nil, nil); err == nil {
		t.Fatal(fmt.Errorf("nil model accepted"))
	}
}
