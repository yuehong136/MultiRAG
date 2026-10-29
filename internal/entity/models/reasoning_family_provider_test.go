package models

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"
)

func TestReasoningFamilyProviderConsumers(t *testing.T) {
	for _, provider := range []string{"Gitee", "SiliconFlow"} {
		for _, tt := range []struct {
			name       string
			model      string
			class      *string
			structured bool
			thinking   bool
			wantSplit  bool
		}{
			{name: "derived namespaced family", model: "qwen/qwen3-8b", thinking: true, wantSplit: true},
			{name: "explicit namespaced dot family", model: "served", class: stringPtr(" Qwen/Qwen3.5-4B "), thinking: true, wantSplit: true},
			{name: "disabled thinking preserved", model: "qwen/qwen3-8b", thinking: false, wantSplit: true},
			{name: "qwen30 remains unknown", model: "qwen30-8b", thinking: true},
			{name: "explicit unknown overrides model", model: "qwen/qwen3-8b", class: stringPtr("qwen3foo"), thinking: true},
			{name: "structured reasoning precedes inline", model: "qwen/qwen3-8b", structured: true, thinking: true},
		} {
			t.Run(provider+"/"+tt.name, func(t *testing.T) {
				content := "<think>\ninline</think>\nanswer"
				srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.Method != http.MethodPost || r.URL.Path != "/chat/completions" || r.Header.Get("Authorization") != "Bearer family-key" {
						t.Error("provider request method/path/auth mismatch")
						w.WriteHeader(http.StatusBadRequest)
						return
					}
					var body map[string]any
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
						return
					}
					if body["model"] != tt.model || body["stream"] != false {
						t.Error("model or stream changed", body["model"], body["stream"])
					}
					assertFamilyThinkingRequest(t, body, tt.thinking)
					message := map[string]any{"content": content}
					if tt.structured {
						message["reasoning_content"] = "structured"
					}
					json.NewEncoder(w).Encode(map[string]any{"choices": []any{map[string]any{"message": message}}})
				}))
				driver, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": srv.URL}, URLSuffix{Chat: "chat/completions", AsyncChat: "async/chat/completions"})
				if err != nil {
					t.Fatal(err)
				}
				thinking := tt.thinking
				config := &ChatConfig{ModelClass: tt.class, Thinking: &thinking}
				response, err := driver.ChatWithMessages(tt.model, &APIConfig{APIKey: stringPtr("family-key"), Context: t.Context()}, []Message{{Role: "user", Content: "ping"}}, config)
				if err != nil {
					t.Fatal(err)
				}
				wantAnswer := content
				var wantReason *string
				if tt.structured {
					wantReason = stringPtr("structured")
				} else if tt.wantSplit {
					wantAnswer, wantReason = "answer", stringPtr("inline")
				}
				assertStringPointer(t, response.Answer, &wantAnswer)
				assertStringPointer(t, response.ReasoningContent, wantReason)
				if config.ModelClass != tt.class || *config.Thinking != tt.thinking {
					t.Fatal("provider mutated caller configuration")
				}
			})
		}
	}
}

func TestReasoningFamilyProvidersStreamThinkingFalse(t *testing.T) {
	for _, provider := range []string{"Gitee", "SiliconFlow"} {
		t.Run(provider, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				assertFamilyThinkingRequest(t, body, false)
				if body["stream"] != true {
					t.Error("stream flag missing")
				}
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\",\"reasoning_content\":\"reason\"}}]}\n\ndata: [DONE]\n\n")
			}))
			driver, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": srv.URL}, URLSuffix{Chat: "chat/completions"})
			if err != nil {
				t.Fatal(err)
			}
			thinking := false
			var answer, reason strings.Builder
			done := 0
			err = driver.ChatStreamlyWithMessages("qwen/qwen3-8b", []Message{{Role: "user", Content: "ping"}}, &APIConfig{APIKey: stringPtr("family-key"), Context: t.Context()}, &ChatConfig{Thinking: &thinking}, func(content, reasoning *string) error {
				if content != nil {
					if *content == "[DONE]" {
						done++
					} else {
						answer.WriteString(*content)
					}
				}
				if reasoning != nil {
					reason.WriteString(*reasoning)
				}
				return nil
			})
			if err != nil || answer.String() != "answer" || reason.String() != "reason" || done != 1 {
				t.Fatal("stream output lost", answer.String(), reason.String(), done, err)
			}
		})
	}
}

func assertFamilyThinkingRequest(t *testing.T, body map[string]any, enabled bool) {
	t.Helper()
	thinking, ok := body["thinking"].(map[string]any)
	want := "disabled"
	if enabled {
		want = "enabled"
	}
	if !ok || thinking["type"] != want {
		t.Errorf("thinking=%v, want type=%s", body["thinking"], want)
	}
}
