package models

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

// The original 22-provider scanner matrix also covers JieKouAI, whose shared
// reader keeps the same large-frame, terminal, and cancellation boundaries.
func after53StreamProviders() []string {
	return []string{"302.AI", "Aliyun", "Baichuan", "Baidu", "Cohere", "DeepInfra", "DeepSeek", "Gitee", "HuggingFace", "JieKouAI", "LM-Studio", "Minimax", "Moonshot", "NVIDIA", "Ollama", "OpenRouter", "OrcaRouter", "SiliconFlow", "TokenHub", "vllm", "VolcEngine", "XunFei", "ZHIPU-AI"}
}

func after53Wire(provider, content, reasoning string, terminal bool) string {
	marshal := func(v any) string {
		raw, err := json.Marshal(v)
		if err != nil {
			panic(err)
		}
		return string(raw)
	}
	switch provider {
	case "Ollama":
		wire := marshal(map[string]any{"message": map[string]any{"content": content, "thinking": reasoning}, "done": false}) + "\n"
		if terminal {
			wire += "{\"message\":{},\"done\":true}\n"
		}
		return wire
	case "Cohere":
		wire := "data: " + marshal(map[string]any{"type": "content-delta", "delta": map[string]any{"message": map[string]any{"content": map[string]any{"text": content, "thinking": reasoning}}}}) + "\n\n"
		if terminal {
			wire += "data: {\"type\":\"message-end\",\"delta\":{\"finish_reason\":\"COMPLETE\"}}\n\n"
		}
		return wire
	default:
		wire := "data: " + marshal(map[string]any{"choices": []any{map[string]any{"delta": map[string]any{"content": content, "reasoning_content": reasoning}}}}) + "\n\n"
		if terminal {
			wire += "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n"
		}
		return wire
	}
}

func TestAfter53StreamingLargeFramesAndFailures(t *testing.T) {
	for _, provider := range after53StreamProviders() {
		for _, method := range []string{"sender", "history"} {
			for _, scenario := range []string{"128KiB", "over1MiB", "over4MiB", "truncated", "sender-error", "cancel"} {
				t.Run(provider+"/"+method+"/"+scenario, func(t *testing.T) {
					size := 128 * 1024
					if scenario == "over1MiB" {
						size = 1024*1024 + 128
					}
					if scenario == "over4MiB" {
						size = 4*1024*1024 + 128
					}
					content, reason := strings.Repeat("a", size), strings.Repeat("r", 70*1024)
					terminal := scenario != "truncated" && scenario != "cancel"
					wire := after53Wire(provider, content, reason, terminal)
					ctx, cancel := context.WithCancel(t.Context())
					defer cancel()
					stopped := make(chan struct{})
					srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						defer close(stopped)
						var body struct {
							Messages []Message `json:"messages"`
						}
						if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
							t.Error(err)
							return
						}
						wantMessages := 1
						if method == "history" {
							wantMessages = 2
						}
						if len(body.Messages) != wantMessages {
							t.Error("history lost", body.Messages)
						}
						if provider == "Ollama" {
							w.Header().Set("Content-Type", "application/x-ndjson")
						} else {
							w.Header().Set("Content-Type", "text/event-stream")
						}
						io.WriteString(w, wire)
						w.(http.Flusher).Flush()
						if scenario == "cancel" {
							select {
							case <-r.Context().Done():
							case <-time.After(3 * time.Second):
								t.Error("provider not canceled")
							}
						}
					}))
					d, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": srv.URL}, URLSuffix{Chat: "chat/completions", Models: "models"})
					if err != nil {
						t.Fatal(err)
					}
					var got, reasoning strings.Builder
					done := 0
					sentinel := errors.New("owned sender stopped")
					sender := func(c, r *string) error {
						if scenario == "sender-error" {
							return sentinel
						}
						if c != nil {
							if *c == "[DONE]" {
								done++
							} else {
								got.WriteString(*c)
							}
						}
						if r != nil {
							reasoning.WriteString(*r)
						}
						if scenario == "cancel" {
							cancel()
						}
						return nil
					}
					api := &APIConfig{APIKey: stringPtr("key"), Context: ctx}
					if method == "sender" {
						err = d.ChatStreamlyWithSender(stringPtr("served"), stringPtr("q"), api, &ChatConfig{}, sender)
					} else {
						err = d.ChatStreamlyWithMessages("served", []Message{{Role: "system", Content: "rules"}, {Role: "user", Content: "q"}}, api, &ChatConfig{}, sender)
					}
					switch scenario {
					case "128KiB", "over1MiB":
						if err != nil || got.String() != content || reasoning.String() != reason || done != 1 {
							t.Fatalf("content=%d/%d reasoning=%d/%d done=%d err=%v", got.Len(), len(content), reasoning.Len(), len(reason), done, err)
						}
					case "sender-error":
						if !errors.Is(err, sentinel) || done != 0 {
							t.Fatal(err, done)
						}
					case "cancel":
						if !errors.Is(err, context.Canceled) || done != 0 {
							t.Fatal(err, done)
						}
					case "over4MiB":
						if !errors.Is(err, bufio.ErrTooLong) || got.Len() != 0 || reasoning.Len() != 0 || done != 0 {
							t.Fatal("oversized frame was partially emitted or scanner error lost", err, got.Len(), reasoning.Len(), done)
						}
					default:
						if err == nil || done != 0 {
							t.Fatal("false completion", err, done)
						}
					}
					select {
					case <-stopped:
					case <-time.After(4 * time.Second):
						t.Fatal("provider handler leaked")
					}
				})
			}
		}
	}
}

func TestAfter53PaddleJSONLLargeLines(t *testing.T) {
	for _, size := range []int{128 * 1024, 1024*1024 + 128, MaxMediaBytes + 1} {
		t.Run(fmt.Sprint(size), func(t *testing.T) {
			text := strings.Repeat("p", size)
			raw, _ := json.Marshal(map[string]any{"result": map[string]any{"layoutParsingResults": []any{map[string]any{"markdown": map[string]any{"text": text}}}}})
			result, err := readPaddleJSONL(strings.NewReader(string(raw) + "\n"))
			if size > MaxMediaBytes {
				if err == nil || result != nil {
					t.Fatal("over limit accepted", err)
				}
				return
			}
			if err != nil || result == nil || result.Text == nil || *result.Text != text {
				t.Fatal("large JSONL lost", err)
			}
		})
	}
	if result, err := readPaddleJSONL(strings.NewReader(`{"result":{"layoutParsingResults":[{"markdown":{"text":"partial"}}]}}` + "\n" + `{"result":`)); err == nil || result != nil {
		t.Fatal("partial JSONL succeeded", result, err)
	}
}
